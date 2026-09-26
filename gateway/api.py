"""HTTP 调用协议。

路由：
- POST /functions/{name}/versions/{version}   注册（不可变）版本
- PUT  /functions/{name}/aliases/{alias}      别名指向版本
- POST /functions/{name}/enable | disable     启停
- POST /invoke/{name}/{version_or_alias}      同步调用（含 request_id 去重）
- POST /invoke-async/{name}/{version_or_alias} 异步调用
- GET  /requests/{request_id}                 查询结论

注册/解析类错误统一映射为 HTTP 409（冲突）或 404，
响应体 ``error.code`` 为可区分的稳定原因码；执行类结论放在调用结果中。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .builtins import BUILTINS
from .errors import GatewayError
from .invocation import Gateway
from .models import InvocationState


class RegisterRequest(BaseModel):
    builtin: str = "echo"
    timeout_seconds: float = 5.0
    concurrency_limit: int = 1


class AliasRequest(BaseModel):
    version: str


class InvokeRequest(BaseModel):
    payload: Any = None
    request_id: str


_CONFLICT_CODES = {
    "version_already_exists",
    "function_disabled",
}


def create_gateway() -> Gateway:
    return Gateway()


def _raise_gateway_error(exc: GatewayError) -> None:
    status = 409 if exc.code in _CONFLICT_CODES else 404
    raise HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


def build_router(gateway: Gateway) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/builtins")
    def list_builtins() -> dict[str, list[str]]:
        return {"builtins": sorted(BUILTINS)}

    @router.post("/functions/{name}/versions/{version}")
    def register_version(name: str, version: str, body: RegisterRequest) -> dict[str, Any]:
        func = BUILTINS.get(body.builtin)
        if func is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "builtin_not_found", "message": body.builtin},
            )
        try:
            ver = gateway.registry.register(
                name,
                version,
                func,
                timeout_seconds=body.timeout_seconds,
                concurrency_limit=body.concurrency_limit,
            )
        except GatewayError as exc:
            _raise_gateway_error(exc)
        return {
            "name": ver.name,
            "version": ver.version,
            "builtin": body.builtin,
            "timeout_seconds": ver.timeout_seconds,
            "concurrency_limit": ver.concurrency_limit,
            "immutable": True,
        }

    @router.put("/functions/{name}/aliases/{alias}")
    def set_alias(name: str, alias: str, body: AliasRequest) -> dict[str, str]:
        try:
            gateway.registry.set_alias(name, alias, body.version)
        except GatewayError as exc:
            _raise_gateway_error(exc)
        return {"name": name, "alias": alias, "version": body.version}

    @router.post("/functions/{name}/enable")
    def enable(name: str) -> dict[str, str]:
        try:
            gateway.registry.enable(name)
        except GatewayError as exc:
            _raise_gateway_error(exc)
        return {"name": name, "status": "enabled"}

    @router.post("/functions/{name}/disable")
    def disable(name: str) -> dict[str, str]:
        try:
            gateway.registry.disable(name)
        except GatewayError as exc:
            _raise_gateway_error(exc)
        return {"name": name, "status": "disabled"}

    def _do_invoke(name: str, target: str, body: InvokeRequest, async_: bool) -> dict[str, Any]:
        if not body.request_id:
            raise HTTPException(
                status_code=400, detail={"code": "missing_request_id", "message": "request_id 必填"}
            )
        if async_:
            result = gateway.invoke_async(name, target, body.payload, request_id=body.request_id)
        else:
            result = gateway.invoke_sync(name, target, body.payload, request_id=body.request_id)
        return result.to_dict()

    @router.post("/invoke/{name}/{target}")
    def invoke_sync(name: str, target: str, body: InvokeRequest) -> dict[str, Any]:
        return _do_invoke(name, target, body, async_=False)

    @router.post("/invoke-async/{name}/{target}")
    def invoke_async(name: str, target: str, body: InvokeRequest) -> dict[str, Any]:
        return _do_invoke(name, target, body, async_=True)

    @router.get("/requests/{request_id}")
    def query_request(request_id: str) -> dict[str, Any]:
        try:
            return gateway.query(request_id).to_dict()
        except GatewayError as exc:
            _raise_gateway_error(exc)
        return {}  # 不可达

    return router
