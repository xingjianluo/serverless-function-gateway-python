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
import random
import threading
from typing import Any, Callable

from .errors import (
    AliasNotFoundError,
    FunctionDisabledError,
    FunctionNotFoundError,
    InvalidRequestError,
    RoutingEmptyError,
    VersionAlreadyExistsError,
    VersionNotFoundError,
    WeightNegativeError,
    WeightSumInvalidError,
)
from .types import FunctionVersion, Target, TargetKind, VersionConfig

logger = logging.getLogger("gateway.registry")


class FunctionRegistry:
    """Thread-safe registry of immutable function versions and aliases."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._lock = threading.RLock()
        # function_name -> version -> FunctionVersion
        self._versions: dict[str, dict[str, FunctionVersion]] = {}
        # function_name -> alias -> {version: weight_percent}
        # A classic single-version alias is stored as {version: 100}.
        self._aliases: dict[str, dict[str, dict[str, int]]] = {}
        # function_name -> enabled flag (absent means never registered)
        self._enabled: dict[str, bool] = {}
        # Weighted picks happen under self._lock; one Random keeps the
        # selection reproducible in tests and off the global module state.
        self._rng = rng or random.Random()

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
                weights = aliases.get(target.name)
                if weights is None:
                    raise AliasNotFoundError(
                        f"alias {target.name!r} of {function_name!r} "
                        "does not exist",
                        detail={
                            "function": function_name,
                            "alias": target.name,
                        },
                    )
                pointed = self._pick_weighted(weights)
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
                if len(weights) > 1:
                    logger.info(
                        "registry.weighted_pick function=%s alias=%s "
                        "weights=%s chosen=%s reason=weighted_alias_resolved",
                        function_name,
                        target.name,
                        weights,
                        pointed,
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
        """Point an alias at a registered version (atomic switch).

        Equivalent to ``set_alias_weights(..., {version: 100})``.
        """
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
            self._aliases.setdefault(function_name, {})[alias] = {
                version: 100
            }
        logger.info(
            "registry.alias_switch function=%s alias=%s %s->%s "
            "reason=explicit_set_alias atomic=true",
            function_name,
            alias,
            old,
            {version: 100},
        )

    def get_alias(self, function_name: str, alias: str) -> str:
        """Return the version an alias currently points at.

        Only valid for single-version aliases; a weighted alias must be
        read with :meth:`get_alias_weights`.
        """
        with self._lock:
            weights = self._alias_weights_locked(function_name, alias)
            if len(weights) != 1:
                raise InvalidRequestError(
                    f"alias {alias!r} of {function_name!r} splits traffic "
                    "across several versions; use get_alias_weights",
                    detail={
                        "function": function_name,
                        "alias": alias,
                        "weights": dict(weights),
                    },
                )
            return next(iter(weights))

    # -- weighted routing (grey rollout) ------------------------------------

    def set_alias_weights(
        self, function_name: str, alias: str, weights: dict[str, int]
    ) -> None:
        """Point an alias at several versions with integer percentages.

        The weights must sum to exactly 100; a 0-weight version keeps its
        in-flight calls but receives no new traffic. Takes effect
        immediately for new resolutions.

        Rejection codes (all distinguishable):
        * ``ROUTING_EMPTY``      -- no version configured at all
        * ``WEIGHT_NEGATIVE``    -- a weight below 0
        * ``WEIGHT_SUM_INVALID`` -- weights do not add up to exactly 100
        * ``VERSION_NOT_FOUND``  -- a weight targets an unpublished version
        * ``INVALID_REQUEST``    -- non-integer weight / bad names
        """
        self._validate_names(function_name, alias)
        if not isinstance(weights, dict) or not weights:
            raise RoutingEmptyError(
                f"alias {alias!r} of {function_name!r} must route to at "
                "least one version",
                detail={"function": function_name, "alias": alias},
            )
        for version, weight in weights.items():
            if (
                isinstance(weight, bool)
                or not isinstance(weight, int)
                or weight > 100
            ):
                raise InvalidRequestError(
                    f"weight for {function_name}:{version} must be an "
                    f"integer in [0, 100], got {weight!r}",
                    detail={"function": function_name, "version": version},
                )
            if weight < 0:
                raise WeightNegativeError(
                    f"weight for {function_name}:{version} must not be "
                    f"negative, got {weight}",
                    detail={
                        "function": function_name,
                        "version": version,
                        "weight": weight,
                    },
                )
        total = sum(weights.values())
        if total != 100:
            raise WeightSumInvalidError(
                f"weights for alias {alias!r} of {function_name!r} add up "
                f"to {total}, expected exactly 100",
                detail={
                    "function": function_name,
                    "alias": alias,
                    "weights": dict(weights),
                    "sum": total,
                },
            )

        with self._lock:
            versions = self._versions.get(function_name)
            if versions is None:
                raise FunctionNotFoundError(
                    f"function {function_name!r} is not registered",
                    detail={"function": function_name},
                )
            for version in weights:
                if version not in versions:
                    raise VersionNotFoundError(
                        f"cannot route alias {alias!r} to unpublished "
                        f"{function_name}:{version}",
                        detail={"function": function_name, "version": version},
                    )
            old = self._aliases.get(function_name, {}).get(alias)
            snapshot = dict(weights)
            self._aliases.setdefault(function_name, {})[alias] = snapshot
        logger.info(
            "registry.alias_weights function=%s alias=%s %s->%s "
            "reason=explicit_set_alias_weights atomic=true "
            "scope=new_invocations_only",
            function_name,
            alias,
            old,
            snapshot,
        )

    def get_alias_weights(
        self, function_name: str, alias: str
    ) -> dict[str, int]:
        """Return the current {version: weight} map of an alias."""
        with self._lock:
            return dict(self._alias_weights_locked(function_name, alias))

    def list_alias_weights(
        self, function_name: str
    ) -> dict[str, dict[str, int]]:
        """Return every alias of a function as {alias: {version: weight}}."""
        with self._lock:
            if function_name not in self._versions:
                raise FunctionNotFoundError(
                    f"function {function_name!r} is not registered",
                    detail={"function": function_name},
                )
            return {
                alias: dict(weights)
                for alias, weights in self._aliases.get(
                    function_name, {}
                ).items()
            }

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
        """Single-version aliases as {alias: version} (legacy view).

        Aliases that split traffic across versions are omitted here; use
        :meth:`list_alias_weights` for the complete view.
        """
        with self._lock:
            if function_name not in self._versions:
                raise FunctionNotFoundError(
                    f"function {function_name!r} is not registered",
                    detail={"function": function_name},
                )
            return {
                alias: next(iter(weights))
                for alias, weights in self._aliases.get(
                    function_name, {}
                ).items()
                if len(weights) == 1
            }

    def version_configs(self, function_name: str) -> dict[str, VersionConfig]:
        """Snapshot of {version: config} for every published version."""
        with self._lock:
            versions = self._versions.get(function_name)
            if versions is None:
                raise FunctionNotFoundError(
                    f"function {function_name!r} is not registered",
                    detail={"function": function_name},
                )
            return {v: fv.config for v, fv in versions.items()}

    def is_enabled(self, function_name: str) -> bool:
        with self._lock:
            return self._enabled.get(function_name, False)

    # -- helpers -----------------------------------------------------------

    def _alias_weights_locked(
        self, function_name: str, alias: str
    ) -> dict[str, int]:
        """Read an alias weight map; caller must hold ``self._lock``."""
        versions = self._versions.get(function_name)
        if versions is None:
            raise FunctionNotFoundError(
                f"function {function_name!r} is not registered",
                detail={"function": function_name},
            )
        weights = self._aliases.get(function_name, {}).get(alias)
        if weights is None:
            raise AliasNotFoundError(
                f"alias {alias!r} of {function_name!r} does not exist",
                detail={"function": function_name, "alias": alias},
            )
        return weights

    def _pick_weighted(self, weights: dict[str, int]) -> str:
        """Choose a version proportionally to its weight.

        A 0-weight version spans an empty interval and is never chosen,
        so draining a version takes effect immediately for new calls.
        Caller must hold ``self._lock`` (keeps the rng single-threaded).
        """
        roll = self._rng.random() * 100.0
        cumulative = 0.0
        chosen = ""
        for version in sorted(weights):
            cumulative += weights[version]
            chosen = version
            if roll < cumulative:
                break
        return chosen

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
