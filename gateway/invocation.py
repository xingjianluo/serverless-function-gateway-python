"""调用门面：请求标识去重、同步/异步调用、结果查询。

幂等（结论收敛）约定：
- 同一 ``request_id`` 在整个进程内只执行一次；
- 并发重复提交时，只有一个提交者真正执行，其余等待/复用同一结果对象；
- 无论同步还是异步、重复多少次，结论（状态、目标版本、载荷）完全一致。

生命周期约定：
- 新请求在“解析阶段”（未发布版本、别名缺失、函数停用）即被拒绝时，
  同步/异步都映射为可区分的终态 REJECTED_*，并记录拒绝原因码；
- 异步调用在后台线程执行，立即返回 RUNNING，可用 query 轮询；
- 解析发生在真正执行线程内，因此下线/切换只影响尚未开始的新请求，
  在途调用持有的是不可变版本对象，结果不会串版本。
"""

from __future__ import annotations

import logging
import threading

from .errors import (
    AliasNotFoundError,
    FunctionDisabledError,
    FunctionNotFoundError,
    GatewayError,
    UnknownRequestError,
    VersionNotFoundError,
)
from .executor import Executor
from .models import InvocationResult, InvocationState
from .registry import Registry

logger = logging.getLogger("gateway")

# 解析阶段拒绝原因 -> 终态
_RESOLVE_REJECTION = {
    FunctionDisabledError: InvocationState.REJECTED_DISABLED,
    FunctionNotFoundError: InvocationState.REJECTED_NO_VERSION,
    VersionNotFoundError: InvocationState.REJECTED_NO_VERSION,
    AliasNotFoundError: InvocationState.REJECTED_NO_VERSION,
}


class Gateway:
    """对外聚合服务：注册中心 + 执行器 + 请求去重表。"""

    def __init__(self, registry: Registry | None = None) -> None:
        self.registry = registry or Registry()
        self.executor = Executor()
        self._requests: dict[str, InvocationResult] = {}
        self._events: dict[str, threading.Event] = {}
        self._guard = threading.Lock()

    # ---- 对外 API ----
    def invoke_sync(
        self,
        name: str,
        version_or_alias: str,
        payload: object = None,
        *,
        request_id: str,
    ) -> InvocationResult:
        """同步调用：阻塞直到出现终态；同一 request_id 返回同一结论。"""
        if not self._claim(request_id):
            logger.info("[req=%s] sync 命中去重，等待已有执行结论", request_id)
            self._events[request_id].wait()
            return self._requests[request_id]

        result = self._execute(name, version_or_alias, payload, request_id)
        self._finish(request_id, result)
        return result

    def invoke_async(
        self,
        name: str,
        version_or_alias: str,
        payload: object = None,
        *,
        request_id: str,
    ) -> InvocationResult:
        """异步调用：后台执行；立即返回 RUNNING（或去重后的既有结论）。"""
        if not self._claim(request_id):
            logger.info("[req=%s] async 命中去重，复用既有结果对象", request_id)
            return self._requests[request_id]

        placeholder = InvocationResult(
            request_id=request_id,
            name=name,
            target="",
            via_alias=None,
            state=InvocationState.RUNNING,
        )
        placeholder.transitions.append(
            "0.0000s state=RUNNING 依据：异步请求已受理，进入后台队列"
        )
        self._requests[request_id] = placeholder

        def runner() -> None:
            final = self._execute(name, version_or_alias, payload, request_id)
            # 原地改写占位对象，保证任何已持有该对象的调用方看到同一结论
            placeholder.target = final.target
            placeholder.via_alias = final.via_alias
            placeholder.state = final.state
            placeholder.payload = final.payload
            placeholder.started_at = final.started_at
            placeholder.finished_at = final.finished_at
            placeholder.transitions.extend(final.transitions)
            self._signal(request_id)

        threading.Thread(
            target=runner, name=f"async-{request_id}", daemon=True
        ).start()
        return placeholder

    def query(self, request_id: str) -> InvocationResult:
        """查询请求结论（含 RUNNING）；未知 request_id 抛 UnknownRequestError。"""
        with self._guard:
            result = self._requests.get(request_id)
        if result is None:
            raise UnknownRequestError(
                f"request_id={request_id} 从未提交", request_id=request_id
            )
        return result

    # ---- 内部 ----
    def _claim(self, request_id: str) -> bool:
        """原子地登记一个新请求。

        返回 True 表示当前调用方是首个提交者，负责真正执行；
        返回 False 表示该 request_id 已存在（含正在执行），必须复用结论。
        """
        with self._guard:
            if request_id in self._requests:
                return False
            self._events[request_id] = threading.Event()
            # 先占一个占位条目，避免“拿到锁之后、插入之前”的窗口
            self._requests[request_id] = InvocationResult(
                request_id=request_id,
                name="",
                target="",
                via_alias=None,
                state=InvocationState.RUNNING,
            )
            return True

    def _finish(self, request_id: str, result: InvocationResult) -> None:
        with self._guard:
            self._requests[request_id] = result
            event = self._events[request_id]
        event.set()

    def _signal(self, request_id: str) -> None:
        with self._guard:
            event = self._events[request_id]
        event.set()

    def _execute(
        self,
        name: str,
        version_or_alias: str,
        payload: object,
        request_id: str,
    ) -> InvocationResult:
        """解析目标版本并执行；解析阶段的拒绝映射为可区分终态。"""
        try:
            ver, via_alias = self.registry.resolve(name, version_or_alias)
        except GatewayError as exc:
            state = _RESOLVE_REJECTION[type(exc)]
            via_alias = version_or_alias if isinstance(exc, AliasNotFoundError) else None
            result = InvocationResult(
                request_id=request_id,
                name=name,
                target=version_or_alias,
                via_alias=via_alias,
                state=state,
                payload=f"{exc.code}: {exc.message}",
            )
            result.transitions.append(
                f"state={state.value} 依据：解析阶段被拒绝，原因码={exc.code}"
            )
            logger.info(
                "[req=%s] resolve 拒绝 state=%s reason=%s",
                request_id,
                state.value,
                exc.code,
            )
            return result

        basis = "按版本直连" if via_alias is None else f"经别名 {via_alias} 解析"
        logger.info(
            "[req=%s] resolve 命中 fn=%s ver=%s (%s)",
            request_id,
            name,
            ver.version,
            basis,
        )
        return self.executor.run(
            ver, request_id=request_id, payload=payload, via_alias=via_alias
        )
