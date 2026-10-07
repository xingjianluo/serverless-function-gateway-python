"""HTTP transport exposing the gateway over FastAPI.

Status mapping (rejections are always explicit, never silent):

* 409 VERSION_ALREADY_EXISTS / REQUEST_CONFLICT
* 404 FUNCTION_NOT_FOUND / VERSION_NOT_FOUND / ALIAS_NOT_FOUND
* 409 FUNCTION_DISABLED
* 429 THROTTLED (concurrency cap)
* 504 TIMED_OUT (handler deadline exceeded)
* 502 HANDLER_ERROR (handler raised)
* 400 INVALID_REQUEST
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import GatewayError
from .gateway import Gateway
from .types import Target, TargetKind

# Terminal state -> HTTP status for the synchronous invoke response.
_STATE_STATUS = {
    "SUCCEEDED": 200,
    "FAILED": 502,
    "TIMED_OUT": 504,
    "THROTTLED": 429,
}

_ERROR_STATUS = {
    "INVALID_REQUEST": 400,
    "ROUTING_EMPTY": 400,
    "WEIGHT_NEGATIVE": 400,
    "WEIGHT_SUM_INVALID": 400,
    "VERSION_ALREADY_EXISTS": 409,
    "REQUEST_CONFLICT": 409,
    "FUNCTION_NOT_FOUND": 404,
    "VERSION_NOT_FOUND": 404,
    "ALIAS_NOT_FOUND": 404,
    "FUNCTION_DISABLED": 409,
}


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class RegisterBody(BaseModel):
    version: str = Field(min_length=1)
    timeout_seconds: float = 5.0
    max_concurrency: int = 4


class AliasBody(BaseModel):
    alias: str = Field(min_length=1)
    version: str | None = Field(default=None, min_length=1)
    weights: dict[str, int] | None = None


class InvokeBody(BaseModel):
    payload: Any = None
    request_id: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _record_dict(record: Any) -> dict[str, Any]:
    return {
        "request_id": record.request_id,
        "function": record.function_name,
        "resolved_version": record.resolved_version,
        "state": record.state.value,
        "result": record.result,
        "error": record.error,
        "error_code": record.error_code,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "elapsed_seconds": (
            record.finished_at - record.started_at
            if record.started_at is not None
            and record.finished_at is not None
            else None
        ),
    }


def _error_response(exc: GatewayError) -> JSONResponse:
    status = _ERROR_STATUS.get(exc.code, 500)
    return JSONResponse(
        status_code=status,
        content={
            "error_code": exc.code,
            "message": str(exc),
            "detail": exc.detail,
        },
    )


def create_app(gateway: Gateway | None = None) -> FastAPI:
    """Build the FastAPI application around a gateway instance."""
    gw = gateway or Gateway()

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        gw.engine.shutdown(wait=False)

    app = FastAPI(
        title="serverless-function-gateway",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.state.gateway = gw
    app.state.handlers = {}

    # ------------------------------------------------------------------
    # Administration
    # ------------------------------------------------------------------

    @app.post("/admin/functions/{name}/versions/{version}")
    def register(name: str, version: str, body: RegisterBody) -> dict[str, Any]:
        """Declare a version. The callable itself must already exist in the
        local handler registry (see app.demo / http_api.bind_handler)."""
        handler = app.state.handlers.get((name, version))
        if handler is None:
            return JSONResponse(
                status_code=400,
                content={
                    "error_code": "INVALID_REQUEST",
                    "message": (
                        f"no local handler bound for {name}:{version}; "
                        "bind it before registration"
                    ),
                    "detail": {"function": name, "version": version},
                },
            )
        app.state.gateway.register_function(
            name,
            version,
            handler,
            timeout_seconds=body.timeout_seconds,
            max_concurrency=body.max_concurrency,
        )
        return {
            "function": name,
            "version": version,
            "timeout_seconds": body.timeout_seconds,
            "max_concurrency": body.max_concurrency,
            "immutable": True,
        }

    @app.put("/admin/functions/{name}/aliases/{alias}")
    def set_alias(name: str, alias: str, body: AliasBody) -> dict[str, Any]:
        if body.weights is not None:
            app.state.gateway.set_alias_weights(name, alias, body.weights)
            return {"function": name, "alias": alias, "weights": body.weights}
        if body.version is None:
            return JSONResponse(
                status_code=400,
                content={
                    "error_code": "INVALID_REQUEST",
                    "message": "provide either 'version' or 'weights'",
                    "detail": {"function": name, "alias": alias},
                },
            )
        app.state.gateway.set_alias(name, alias, body.version)
        return {"function": name, "alias": alias, "version": body.version}

    @app.get("/functions/{name}/overview")
    def function_overview(name: str) -> dict[str, Any]:
        """Weights per alias plus per-version inflight/limit/terminal counts."""
        return app.state.gateway.function_overview(name)

    @app.post("/admin/functions/{name}/status")
    async def set_status(name: str, request: Request) -> dict[str, Any]:
        body = await request.json()
        enabled = bool(body.get("enabled"))
        if enabled:
            app.state.gateway.enable(name)
        else:
            app.state.gateway.disable(name)
        return {"function": name, "enabled": enabled}

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    def _do_invoke(name: str, ref: str, kind: TargetKind, body: InvokeBody,
                   sync: bool) -> Any:
        target = Target(kind=kind, name=ref)
        if sync:
            record = app.state.gateway.invoke(
                name, target, body.payload, request_id=body.request_id
            )
            status = _STATE_STATUS.get(record.state.value, 500)
            return JSONResponse(
                status_code=status if record.state.value != "SUCCEEDED" else 200,
                content=_record_dict(record),
            )
        record = app.state.gateway.invoke_async(
            name, target, body.payload, request_id=body.request_id
        )
        return JSONResponse(status_code=202, content=_record_dict(record))

    @app.post("/functions/{name}/versions/{version}/invoke")
    def invoke_version(
        name: str, version: str, body: InvokeBody
    ) -> Any:
        return _do_invoke(name, version, TargetKind.VERSION, body, sync=True)

    @app.post("/functions/{name}/aliases/{alias}/invoke")
    def invoke_alias(name: str, alias: str, body: InvokeBody) -> Any:
        return _do_invoke(name, alias, TargetKind.ALIAS, body, sync=True)

    @app.post("/functions/{name}/versions/{version}/invoke-async")
    def invoke_version_async(
        name: str, version: str, body: InvokeBody
    ) -> Any:
        return _do_invoke(name, version, TargetKind.VERSION, body, sync=False)

    @app.post("/functions/{name}/aliases/{alias}/invoke-async")
    def invoke_alias_async(name: str, alias: str, body: InvokeBody) -> Any:
        return _do_invoke(name, alias, TargetKind.ALIAS, body, sync=False)

    @app.get("/requests/{request_id}")
    def get_result(request_id: str) -> Any:
        record = app.state.gateway.get_result(request_id)
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error_code": "REQUEST_NOT_FOUND",
                    "message": f"unknown request id {request_id!r}",
                    "detail": {"request_id": request_id},
                },
            )
        return _record_dict(record)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(
        request: Request, exc: GatewayError
    ) -> JSONResponse:
        return _error_response(exc)

    return app
