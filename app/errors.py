"""Error taxonomy for the gateway.

Every rejection carries a stable ``code`` so that callers and tests can
distinguish *why* an operation was refused instead of parsing messages.
"""

from __future__ import annotations


class GatewayError(Exception):
    """Base class for all gateway-level rejections."""

    code = "GATEWAY_ERROR"

    def __init__(self, message: str = "", *, detail: dict | None = None) -> None:
        super().__init__(message or self.code)
        self.detail = detail or {}


class VersionAlreadyExistsError(GatewayError):
    """Re-registering an (function, version) pair that already exists."""

    code = "VERSION_ALREADY_EXISTS"


class VersionNotFoundError(GatewayError):
    """The requested version was never registered for this function."""

    code = "VERSION_NOT_FOUND"


class FunctionNotFoundError(GatewayError):
    """The function name is unknown to the registry."""

    code = "FUNCTION_NOT_FOUND"


class AliasNotFoundError(GatewayError):
    """The alias does not exist for this function."""

    code = "ALIAS_NOT_FOUND"


class FunctionDisabledError(GatewayError):
    """The function is stopped; new invocations are refused."""

    code = "FUNCTION_DISABLED"


class InvalidRequestError(GatewayError):
    """The request itself is malformed (bad payload, bad names, ...)."""

    code = "INVALID_REQUEST"


class RoutingConfigError(GatewayError):
    """Base class for invalid weighted-routing configurations."""


class RoutingEmptyError(RoutingConfigError):
    """A weighted alias was configured with no versions at all."""

    code = "ROUTING_EMPTY"


class WeightSumInvalidError(RoutingConfigError):
    """The weights of a weighted alias do not add up to exactly 100."""

    code = "WEIGHT_SUM_INVALID"


class WeightNegativeError(RoutingConfigError):
    """A weighted alias contained a negative weight."""

    code = "WEIGHT_NEGATIVE"


class RequestConflictError(GatewayError):
    """Same request id submitted with different parameters."""

    code = "REQUEST_CONFLICT"


class ThrottledError(GatewayError):
    """Concurrency limit reached; the call was refused, not queued."""

    code = "THROTTLED"


class InvocationTimeoutError(GatewayError):
    """The execution exceeded its configured time limit."""

    code = "TIMED_OUT"


class InvocationFailedError(GatewayError):
    """The handler raised an exception during execution."""

    code = "HANDLER_ERROR"
