"""Function registry: versions, aliases and enable/disable lifecycle.

The registry owns *what can be invoked*; the engine owns *how it runs*.

Guarantees:
* A registered (function, version) pair is immutable. Re-registration is
  rejected with ``VERSION_ALREADY_EXISTS``.
* Alias switches are atomic: readers see either the old or the new
  version, never a torn pointer.
* Enable/disable only gates *new* admissions; the engine holds the
  resolved :class:`FunctionVersion` for in-flight calls, so a stop or
  alias switch can never make a running call jump to another version.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .errors import (
    AliasNotFoundError,
    FunctionDisabledError,
    FunctionNotFoundError,
    InvalidRequestError,
    VersionAlreadyExistsError,
    VersionNotFoundError,
)
from .types import FunctionVersion, Target, TargetKind, VersionConfig

logger = logging.getLogger("gateway.registry")


class FunctionRegistry:
    """Thread-safe registry of immutable function versions and aliases."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # function_name -> version -> FunctionVersion
        self._versions: dict[str, dict[str, FunctionVersion]] = {}
        # function_name -> alias -> version
        self._aliases: dict[str, dict[str, str]] = {}
        # function_name -> enabled flag (absent means never registered)
        self._enabled: dict[str, bool] = {}

    # -- registration ------------------------------------------------------

    def register(
        self,
        function_name: str,
        version: str,
        handler: Callable[..., Any],
        config: VersionConfig | None = None,
    ) -> FunctionVersion:
        """Register an immutable version. Re-registering is rejected.

        Rejection codes:
        * ``INVALID_REQUEST``      -- bad names/config/handler
        * ``VERSION_ALREADY_EXISTS`` -- the pair was registered before
        """
        self._validate_names(function_name, version)
        if not callable(handler):
            raise InvalidRequestError(
                f"handler for {function_name}:{version} must be callable"
            )
        config = config or VersionConfig()
        if config.timeout_seconds <= 0:
            raise InvalidRequestError("timeout_seconds must be > 0")
        if config.max_concurrency < 1:
            raise InvalidRequestError("max_concurrency must be >= 1")

        with self._lock:
            versions = self._versions.get(function_name)
            if versions is not None and version in versions:
                raise VersionAlreadyExistsError(
                    f"{function_name}:{version} is already registered "
                    "and cannot be modified",
                    detail={"function": function_name, "version": version},
                )

            fv = FunctionVersion(
                function_name=function_name,
                version=version,
                handler=handler,
                config=config,
            )
            self._versions.setdefault(function_name, {})[version] = fv
            # First registration starts the function; later registrations
            # leave its current enabled/disabled state untouched.
            self._enabled.setdefault(function_name, True)

        logger.info(
            "registry.register function=%s version=%s timeout=%.3fs "
            "max_concurrency=%d reason=first_registration_immutable",
            function_name,
            version,
            config.timeout_seconds,
            config.max_concurrency,
        )
        return fv

    # -- resolution --------------------------------------------------------

    def resolve(self, function_name: str, target: Target) -> FunctionVersion:
        """Resolve a target to a concrete version, enforcing lifecycle.

        The returned object is captured by the engine before execution,
        so later alias/lifecycle changes cannot affect this invocation.

        Rejection codes:
        * ``FUNCTION_NOT_FOUND``, ``VERSION_NOT_FOUND``,
          ``ALIAS_NOT_FOUND``, ``FUNCTION_DISABLED``
        """
        with self._lock:
            versions = self._versions.get(function_name)
            if versions is None:
                raise FunctionNotFoundError(
                    f"function {function_name!r} is not registered",
                    detail={"function": function_name},
                )

            if target.kind is TargetKind.VERSION:
                fv = versions.get(target.name)
                if fv is None:
                    raise VersionNotFoundError(
                        f"{function_name}:{target.name} is not published",
                        detail={
                            "function": function_name,
                            "version": target.name,
                        },
                    )
            elif target.kind is TargetKind.ALIAS:
                aliases = self._aliases.get(function_name, {})
                pointed = aliases.get(target.name)
                if pointed is None:
                    raise AliasNotFoundError(
                        f"alias {target.name!r} of {function_name!r} "
                        "does not exist",
                        detail={
                            "function": function_name,
                            "alias": target.name,
                        },
                    )
                fv = versions.get(pointed)
                if fv is None:  # defensive: versions can never be deleted
                    raise VersionNotFoundError(
                        f"alias {target.name!r} points at unpublished "
                        f"version {pointed!r}",
                        detail={
                            "function": function_name,
                            "version": pointed,
                        },
                    )
            else:  # pragma: no cover - exhaustive enum
                raise InvalidRequestError(f"unknown target kind: {target.kind!r}")

            if not self._enabled.get(function_name, False):
                logger.info(
                    "registry.reject function=%s resolved_version=%s "
                    "reason=function_disabled",
                    function_name,
                    fv.version,
                )
                raise FunctionDisabledError(
                    f"function {function_name!r} is stopped",
                    detail={
                        "function": function_name,
                        "resolved_version": fv.version,
                    },
                )

            logger.debug(
                "registry.resolve function=%s target=%s/%s "
                "resolved_version=%s",
                function_name,
                target.kind.value,
                target.name,
                fv.version,
            )
            return fv

    # -- lifecycle ---------------------------------------------------------

    def set_enabled(self, function_name: str, enabled: bool) -> None:
        """Start or stop a function. In-flight calls are unaffected."""
        with self._lock:
            if function_name not in self._versions:
                raise FunctionNotFoundError(
                    f"function {function_name!r} is not registered",
                    detail={"function": function_name},
                )
            previous = self._enabled.get(function_name, True)
            self._enabled[function_name] = enabled
        logger.info(
            "registry.lifecycle function=%s change=%s->%s "
            "reason=explicit_enable_disable scope=new_invocations_only",
            function_name,
            previous,
            enabled,
        )

    def set_alias(self, function_name: str, alias: str, version: str) -> None:
        """Point an alias at a registered version (atomic switch)."""
        self._validate_names(function_name, alias)
        with self._lock:
            versions = self._versions.get(function_name)
            if versions is None:
                raise FunctionNotFoundError(
                    f"function {function_name!r} is not registered",
                    detail={"function": function_name},
                )
            if version not in versions:
                raise VersionNotFoundError(
                    f"cannot bind alias {alias!r} to unpublished "
                    f"{function_name}:{version}",
                    detail={"function": function_name, "version": version},
                )
            old = self._aliases.get(function_name, {}).get(alias)
            self._aliases.setdefault(function_name, {})[alias] = version
        logger.info(
            "registry.alias_switch function=%s alias=%s %s->%s "
            "reason=explicit_set_alias atomic=true",
            function_name,
            alias,
            old,
            version,
        )

    def get_alias(self, function_name: str, alias: str) -> str:
        """Return the version an alias currently points at."""
        with self._lock:
            versions = self._versions.get(function_name)
            if versions is None:
                raise FunctionNotFoundError(
                    f"function {function_name!r} is not registered",
                    detail={"function": function_name},
                )
            pointed = self._aliases.get(function_name, {}).get(alias)
            if pointed is None:
                raise AliasNotFoundError(
                    f"alias {alias!r} of {function_name!r} does not exist",
                    detail={"function": function_name, "alias": alias},
                )
            return pointed

    # -- introspection -----------------------------------------------------

    def list_versions(self, function_name: str) -> list[str]:
        with self._lock:
            versions = self._versions.get(function_name)
            if versions is None:
                raise FunctionNotFoundError(
                    f"function {function_name!r} is not registered",
                    detail={"function": function_name},
                )
            return sorted(versions)

    def list_aliases(self, function_name: str) -> dict[str, str]:
        with self._lock:
            if function_name not in self._versions:
                raise FunctionNotFoundError(
                    f"function {function_name!r} is not registered",
                    detail={"function": function_name},
                )
            return dict(self._aliases.get(function_name, {}))

    def is_enabled(self, function_name: str) -> bool:
        with self._lock:
            return self._enabled.get(function_name, False)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _validate_names(function_name: str, version_or_alias: str) -> None:
        if not isinstance(function_name, str) or not function_name.strip():
            raise InvalidRequestError("function_name must be a non-empty string")
        if (
            not isinstance(version_or_alias, str)
            or not version_or_alias.strip()
        ):
            raise InvalidRequestError(
                "version/alias must be a non-empty string"
            )
