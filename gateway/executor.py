"""执行器：按版本控制并发上限、超时判定、槽位精确回收。

关键一致性约定：
- 每个 ``(函数名, 版本)`` 维护独立在途计数；超限**立即拒绝**，绝不静默排队；
- 用户函数在独立守护线程中运行，主线程等待至多 ``timeout_seconds``；
- 超时后调用被判定为 TIMED_OUT 并立即把结论交还给调用方；若用户函数
  稍后才真正结束，其占用的槽位由回收线程在函数返回后释放——
  计数只减一次，绝不为负，杜绝并发计数漂移；
- 切换/下线只影响“新请求的解析”，已拿到不可变版本对象的在途调用
  继续执行原版本，不会串入其它版本。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

from .models import FunctionVersion, InvocationResult, InvocationState

logger = logging.getLogger("gateway")


class Executor:
    def __init__(self) -> None:
        # key: (name, version) -> 当前在途数
        self._inflight: dict[tuple[str, str], int] = defaultdict(int)
        self._lock = threading.Lock()

    # ---- 并发槽位 ----
    def try_acquire(self, ver: FunctionVersion) -> bool:
        key = (ver.name, ver.version)
        with self._lock:
            current = self._inflight[key]
            if current >= ver.concurrency_limit:
                return False
            self._inflight[key] = current + 1
            return True

    def release(self, ver: FunctionVersion) -> None:
        key = (ver.name, ver.version)
        with self._lock:
            current = self._inflight.get(key, 0)
            if current <= 0:
                # 防御性保护：正常路径不会走到这里
                logger.error("slot release underflow: %s count=%s", key, current)
                self._inflight[key] = 0
                return
            self._inflight[key] = current - 1

    def inflight(self, ver: FunctionVersion) -> int:
        with self._lock:
            return self._inflight.get((ver.name, ver.version), 0)

    # ---- 执行 ----
    def run(
        self,
        ver: FunctionVersion,
        *,
        request_id: str,
        payload: object,
        via_alias: str | None,
    ) -> InvocationResult:
        started = time.time()
        result = InvocationResult(
            request_id=request_id,
            name=ver.name,
            target=ver.version,
            via_alias=via_alias,
            state=InvocationState.RUNNING,
            started_at=started,
        )

        def log(basis: str) -> None:
            line = f"[req={request_id}] fn={ver.name} target={ver.version} {basis}"
            result.transitions.append(f"{time.time() - started:.4f}s {basis}")
            logger.info(line)

        log("state=RUNNING 依据：请求已受理，准备申请并发槽位")

        if not self.try_acquire(ver):
            result.state = InvocationState.CONCURRENCY_REJECTED
            result.finished_at = time.time()
            log(
                f"state=CONCURRENCY_REJECTED 依据：在途 {ver.concurrency_limit}/"
                f"{ver.concurrency_limit} 已达上限，立即拒绝，不排队"
            )
            return result

        log(
            f"state=RUNNING 依据：获得并发槽位（在途 {self.inflight(ver)}/"
            f"{ver.concurrency_limit}），开始执行用户函数"
        )

        outcome: dict[str, object] = {}
        finished = threading.Event()

        def worker() -> None:
            try:
                outcome["value"] = self.invoke_user_function(ver, payload)
            except BaseException as exc:  # 用户函数异常属于业务失败
                outcome["error"] = exc
            finally:
                finished.set()

        thread = threading.Thread(
            target=worker,
            name=f"fn-{ver.name}-{ver.version}-{request_id}",
            daemon=True,
        )
        thread.start()

        in_time = finished.wait(ver.timeout_seconds)

        if not in_time:
            # 超时：结论立即确定；槽位待孤儿调用真正结束后再回收
            result.state = InvocationState.TIMED_OUT
            result.payload = f"执行超过时限 {ver.timeout_seconds}s"
            result.finished_at = time.time()
            log(
                f"state=TIMED_OUT 依据：等待 {ver.timeout_seconds}s 后仍未完成；"
                "槽位将在用户函数实际返回后回收"
            )

            def reap() -> None:
                finished.wait()
                self.release(ver)
                log(
                    "state=TIMED_OUT 依据：超时调用已实际结束，槽位已回收"
                    f"（在途 {self.inflight(ver)}/{ver.concurrency_limit}）"
                )

            threading.Thread(target=reap, name=f"reap-{request_id}", daemon=True).start()
            return result

        # 在时限内结束：精确释放一次
        self.release(ver)
        result.finished_at = time.time()
        if "error" in outcome:
            exc = outcome["error"]
            result.state = InvocationState.FAILED
            result.payload = f"{type(exc).__name__}: {exc}"
            log(
                f"state=FAILED 依据：用户函数抛出 {type(exc).__name__}: {exc}；"
                f"槽位已回收（在途 {self.inflight(ver)}/{ver.concurrency_limit}）"
            )
        else:
            result.state = InvocationState.SUCCEEDED
            result.payload = outcome.get("value")
            log(
                "state=SUCCEEDED 依据：用户函数正常返回；"
                f"槽位已回收（在途 {self.inflight(ver)}/{ver.concurrency_limit}）"
            )
        return result

    def invoke_user_function(self, ver: FunctionVersion, payload: object) -> object:
        """在工作线程中实际调用用户函数（payload 作为唯一位置参数）。"""
        return ver.func(payload)
