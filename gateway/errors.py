"""可区分的拒绝原因与执行状态。

所有注册/调用阶段的拒绝都使用 :class:`GatewayError` 的子类，
``code`` 字段给出机器可判别的稳定原因码，便于测试与日志解释。
"""

from __future__ import annotations


class GatewayError(Exception):
    """网关层错误基类。"""

    code: str = "gateway_error"

    def __init__(self, message: str = "", *, request_id: str | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.request_id = request_id


# ---- 注册 / 版本类 ----
class VersionAlreadyExistsError(GatewayError):
    """同一函数名+版本已注册，版本一经注册不可篡改。"""

    code = "version_already_exists"


class VersionNotFoundError(GatewayError):
    """调用了从未发布（注册）的版本。"""

    code = "version_not_found"


class FunctionNotFoundError(GatewayError):
    """函数名不存在。"""

    code = "function_not_found"


class AliasNotFoundError(GatewayError):
    """引用的别名不存在。"""

    code = "alias_not_found"


# ---- 生命周期类 ----
class FunctionDisabledError(GatewayError):
    """函数已被停用，拒绝新请求。"""

    code = "function_disabled"


# ---- 执行类（同步调用直接抛出；异步结果中以状态体现） ----
class ConcurrencyLimitRejectedError(GatewayError):
    """并发已达版本上限，拒绝（不静默排队）。"""

    code = "concurrency_limit_rejected"


class InvocationTimeoutError(GatewayError):
    """单次执行超出时限。"""

    code = "invocation_timeout"


class UnknownRequestError(GatewayError):
    """查询了一个从未提交过的 request_id。"""

    code = "unknown_request"
