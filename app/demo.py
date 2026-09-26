"""Demo wiring: register local functions for manual HTTP testing.

The platform runs *local* functions: code is registered in-process, not
uploaded over HTTP. This module is the binding table used by ``main.py``
and by the HTTP layer's registration endpoint.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .gateway import Gateway
from .http_api import create_app
from .types import VersionConfig
from fastapi import FastAPI

# (function, version) -> (callable, config)
HANDLER_TABLE: dict[tuple[str, str], tuple[Callable[..., Any], VersionConfig]] = {
    ("echo", "v1"): (
        lambda payload: {"echo": payload},
        VersionConfig(timeout_seconds=2.0, max_concurrency=8),
    ),
    ("echo", "v2"): (
        lambda payload: {"echo": payload, "version": "v2"},
        VersionConfig(timeout_seconds=2.0, max_concurrency=8),
    ),
    ("slow", "v1"): (
        lambda payload: time.sleep(
            float(payload.get("seconds", 0.5))
            if isinstance(payload, dict)
            else 0.5
        )
        or "done",
        VersionConfig(timeout_seconds=1.0, max_concurrency=2),
    ),
    ("flaky", "v1"): (
        lambda payload: (_ for _ in ()).throw(
            RuntimeError("intentional handler failure")
        ),
        VersionConfig(timeout_seconds=2.0, max_concurrency=4),
    ),
}


def build_gateway() -> Gateway:
    gateway = Gateway()
    for (name, version), (handler, cfg) in HANDLER_TABLE.items():
        gateway.register_function(
            name,
            version,
            handler,
            timeout_seconds=cfg.timeout_seconds,
            max_concurrency=cfg.max_concurrency,
        )
    # Alias demonstrating version switching semantics.
    gateway.set_alias("echo", "current", "v1")
    gateway.set_alias("slow", "current", "v1")
    return gateway


def build_app() -> FastAPI:
    gateway = build_gateway()
    app = create_app(gateway)
    # Expose already-bound handlers for the admin registration endpoint.
    app.state.handlers = {
        key: handler for key, (handler, _cfg) in HANDLER_TABLE.items()
    }
    return app
