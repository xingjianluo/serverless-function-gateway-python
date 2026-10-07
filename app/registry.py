"""Function registry: versions, aliases and enable/disable lifecycle.

The registry owns *what can be invoked*; the engine owns *how it runs*.

Guarantees:
* A registered (function, version) pair is immutable. Re-registration is
  rejected with ``VERSION_ALREADY_EXISTS``.
* An alias routes to one or more versions with integer percentage weights
  summing to exactly 100. A plain single-version alias is the special case
  ``{version: 100}``. Weight updates are atomic: the next resolve sees
  either the old or the new map, never a torn pointer.
* Weighted picks use smooth weighted round-robin, so over each 100-pick
  cycle the distribution matches the configured percentages exactly.
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
    AliasWeightsEmptyError,
    FunctionDisabledError,
    FunctionNotFoundError,
    InvalidRequestError,
    InvalidWeightError,
    VersionAlreadyExistsError,
    VersionNotFoundError,
    WeightSumInvalidError,
)
from .types import FunctionVersion, Target, TargetKind, VersionConfig

logger = logging.getLogger("gateway.registry")


class FunctionRegistry:
    """Thread-safe registry of immutable function versions and aliases."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # function_name -> version -> FunctionVersion
        self._versions: dict[str, dict[str, FunctionVersion]] = {}
        # function_name -> alias -> {version: weight_percent}
        self._aliases: dict[str, dict[str, dict[str, int]]] = {}
        # (function_name, alias) -> {version: current} smooth-WRR state
        self._alias_picker: dict[tuple[str, str], dict[str, int]] = {}
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
                pointed = self._pick_weighted(
                    function_name, target.name, weights
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
        """Point an alias at a single registered version (atomic switch).

        This is the classic single-target form, exactly equivalent to
        ``set_alias_weights(function_name, alias, {version: 100})``.
        """
        self.set_alias_weights(function_name, alias, {version: 100})

    def set_alias_weights(
        self, function_name: str, alias: str, weights: dict[str, int]
    ) -> None:
        """Bind an alias to several versions by integer percentages.

        The weights must be non-negative integers summing to exactly 100;
        a version with weight 0 keeps its slot but receives no traffic.
        The switch is atomic and takes effect on the next resolve;
        in-flight calls keep their already-resolved version.

        Rejection codes (all distinguishable):
        * ``INVALID_REQUEST``       -- bad names / weights not a dict
        * ``ALIAS_WEIGHTS_EMPTY``   -- no version configured at all
        * ``INVALID_WEIGHT``        -- a weight is negative or not an int
        * ``VERSION_NOT_FOUND``     -- a target version was never published
        * ``WEIGHT_SUM_INVALID``    -- the weights do not add up to 100
        * ``FUNCTION_NOT_FOUND``    -- the function is not registered
        """
        self._validate_names(function_name, alias)
        if not isinstance(weights, dict):
            raise InvalidRequestError(
                "weights must be a dict of version -> integer percent"
            )
        if not weights:
            raise AliasWeightsEmptyError(
                f"alias {alias!r} of {function_name!r} must route to at "
                "least one version",
                detail={"function": function_name, "alias": alias},
            )
        clean: dict[str, int] = {}
        for version, weight in weights.items():
            if not isinstance(version, str) or not version.strip():
                raise InvalidRequestError(
                    "weight map keys must be non-empty version names"
                )
            if isinstance(weight, bool) or not isinstance(weight, int):
                raise InvalidWeightError(
                    f"weight for {version!r} must be an integer percent, "
                    f"got {weight!r}",
                    detail={"version": version, "weight": weight},
                )
            if weight < 0:
                raise InvalidWeightError(
                    f"weight for {version!r} must be >= 0, got {weight}",
                    detail={"version": version, "weight": weight},
                )
            clean[version] = weight

        with self._lock:
            versions = self._versions.get(function_name)
            if versions is None:
                raise FunctionNotFoundError(
                    f"function {function_name!r} is not registered",
                    detail={"function": function_name},
                )
            for version in clean:
                if version not in versions:
                    raise VersionNotFoundError(
                        f"cannot bind alias {alias!r} to unpublished "
                        f"{function_name}:{version}",
                        detail={"function": function_name, "version": version},
                    )
            total = sum(clean.values())
            if total != 100:
                raise WeightSumInvalidError(
                    f"weights for alias {alias!r} of {function_name!r} "
                    f"must sum to 100, got {total}",
                    detail={
                        "function": function_name,
                        "alias": alias,
                        "sum": total,
                    },
                )
            old = self._aliases.get(function_name, {}).get(alias)
            self._aliases.setdefault(function_name, {})[alias] = dict(clean)
            # Reset the smooth-WRR counters so the new proportions apply
            # from the very next pick.
            self._alias_picker[(function_name, alias)] = {
                v: 0 for v in clean
            }
        logger.info(
            "registry.alias_switch function=%s alias=%s %s->%s "
            "reason=explicit_set_alias atomic=true",
            function_name,
            alias,
            old,
            clean,
        )

    def get_alias_weights(
        self, function_name: str, alias: str
    ) -> dict[str, int]:
        """Return the current version -> weight map of an alias."""
        with self._lock:
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
            return dict(weights)

    def get_alias(self, function_name: str, alias: str) -> str:
        """Return the version an alias currently points at.

        Only valid for aliases routing 100%% to a single version; for a
        weighted alias use :meth:`get_alias_weights` instead.
        """
        weights = self.get_alias_weights(function_name, alias)
        if len(weights) != 1:
            raise InvalidRequestError(
                f"alias {alias!r} of {function_name!r} routes to "
                f"{len(weights)} versions; use get_alias_weights",
                detail={"function": function_name, "alias": alias},
            )
        return next(iter(weights))

    def _pick_weighted(
        self, function_name: str, alias: str, weights: dict[str, int]
    ) -> str:
        """Smooth weighted round-robin pick (called under ``_lock``).

        Deterministic and exactly proportional: over each 100-pick cycle
        every version is selected precisely ``weight`` times.
        """
        key = (function_name, alias)
        current = self._alias_picker.get(key)
        if current is None or set(current) != set(weights):
            current = {v: 0 for v in weights}
            self._alias_picker[key] = current
        for version, weight in weights.items():
            current[version] += weight
        picked = max(current, key=lambda v: current[v])
        current[picked] -= 100
        return picked

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

    def version_configs(self, function_name: str) -> dict[str, VersionConfig]:
        """Every published version's immutable config, keyed by version."""
        with self._lock:
            versions = self._versions.get(function_name)
            if versions is None:
                raise FunctionNotFoundError(
                    f"function {function_name!r} is not registered",
                    detail={"function": function_name},
                )
            return {
                version: fv.config for version, fv in versions.items()
            }

    def list_aliases(self, function_name: str) -> dict[str, str]:
        """Aliases routing 100% to one version, as ``alias -> version``.

        Weighted aliases are intentionally excluded here (they have no
        single target); use :meth:`list_alias_weights` for the full view.
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

    def list_alias_weights(self, function_name: str) -> dict[str, dict[str, int]]:
        """Every alias of the function as ``alias -> {version: weight}``."""
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
