"""Gateway facade: the single entry point used by transports and tests.

The facade wires the registry and engine together and offers a small,
explicit API. Targets are always expressed through :class:`Target` so that
a literal version and an alias can never be confused.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .engine import InvocationEngine
from .registry import FunctionRegistry
from .types import (
    InvocationRecord,
    InvocationRequest,
    Target,
    TargetKind,
    VersionConfig,
)

logger = logging.getLogger("gateway")


class Gateway:
    """Composes the registry and the engine into one coherent service."""

    def __init__(self, *, pool_size: int = 256) -> None:
        self.registry = FunctionRegistry()
        self.engine = InvocationEngine(self.registry, pool_size=pool_size)

    # -- administration ----------------------------------------------------

    def register_function(
        self,
        name: str,
        version: str,
        handler: Callable[[Any], Any],
        *,
        timeout_seconds: float = 5.0,
        max_concurrency: int = 4,
    ) -> None:
        """Register an immutable function version."""
        self.registry.register(
            name,
            version,
            handler,
            VersionConfig(
                timeout_seconds=timeout_seconds,
                max_concurrency=max_concurrency,
            ),
        )

    def set_alias(self, name: str, alias: str, version: str) -> None:
        """Atomically bind (or move) an alias to a published version."""
        self.registry.set_alias(name, alias, version)

    def enable(self, name: str) -> None:
        self.registry.set_enabled(name, True)

    def disable(self, name: str) -> None:
        self.registry.set_enabled(name, False)

    # -- invocation --------------------------------------------------------

    def invoke(
        self,
        name: str,
        target: Target,
        payload: Any = None,
        *,
        request_id: str,
    ) -> InvocationRecord:
        """Synchronous invocation: returns a terminal record."""
        return self.engine.invoke_sync(
            InvocationRequest(
                request_id=request_id,
                function_name=name,
                target=target,
                payload=payload,
            )
        )

    def invoke_async(
        self,
        name: str,
        target: Target,
        payload: Any = None,
        *,
        request_id: str,
    ) -> InvocationRecord:
        """Asynchronous invocation: returns immediately; poll get_result."""
        return self.engine.invoke_async(
            InvocationRequest(
                request_id=request_id,
                function_name=name,
                target=target,
                payload=payload,
            )
        )

    def get_result(self, request_id: str) -> InvocationRecord | None:
        return self.engine.get_result(request_id)

    def inflight(self, name: str, version: str) -> int:
        return self.engine.inflight_count(name, version)

    # -- convenience constructors -----------------------------------------

    @staticmethod
    def version_target(version: str) -> Target:
        return Target(TargetKind.VERSION, version)

    @staticmethod
    def alias_target(alias: str) -> Target:
        return Target(TargetKind.ALIAS, alias)
