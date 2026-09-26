"""Core type definitions for the serverless function gateway.

All shared value objects and enumerations live here so that every module
agrees on one vocabulary for states, errors and lifecycle decisions.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class InvocationState(enum.Enum):
    """Lifecycle states of an invocation (sync or async)."""

    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    THROTTLED = "THROTTLED"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL_STATES


_TERMINAL_STATES = frozenset(
    {
        InvocationState.SUCCEEDED,
        InvocationState.FAILED,
        InvocationState.TIMED_OUT,
        InvocationState.THROTTLED,
    }
)


class TargetKind(enum.Enum):
    """How an invocation target was specified by the caller."""

    VERSION = "version"
    ALIAS = "alias"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionConfig:
    """Immutable per-version configuration.

    Once a version is registered this object is the single source of truth
    for its limits; it is never mutated afterwards.
    """

    timeout_seconds: float = 5.0
    max_concurrency: int = 4


@dataclass(frozen=True)
class Target:
    """Invocation target: either a concrete version or an alias."""

    kind: TargetKind
    name: str

    @staticmethod
    def version(version: str) -> "Target":
        return Target(TargetKind.VERSION, version)

    @staticmethod
    def alias(alias: str) -> "Target":
        return Target(TargetKind.ALIAS, alias)


@dataclass(frozen=True)
class FunctionVersion:
    """A registered, immutable function version."""

    function_name: str
    version: str
    handler: Callable[..., Any]
    config: VersionConfig
    registered_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class InvocationRequest:
    """A caller's request to invoke a function."""

    request_id: str
    function_name: str
    target: Target
    payload: Any = None


@dataclass(frozen=True)
class InvocationRecord:
    """A point-in-time snapshot of one invocation's outcome."""

    request_id: str
    function_name: str
    resolved_version: str
    state: InvocationState
    result: Any = None
    error: str | None = None
    error_code: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
