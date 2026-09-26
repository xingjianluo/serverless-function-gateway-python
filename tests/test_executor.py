"""并发上限立即拒绝、超时判定、槽位精确回收不漂移。"""

import logging
import threading
import time

from gateway.executor import Executor
from gateway.models import FunctionVersion, InvocationState
from gateway.registry import Registry

logger = logging.getLogger("gateway.test_executor")


def _ver(name="f", version="v1", timeout=5.0, limit=1, func=lambda p: "ok"):
    return FunctionVersion(
        name=name, version=version, func=func,
        timeout_seconds=timeout, concurrency_limit=limit,
    )


def test_success_releases_slot():
    ex = Executor()
    v = _ver()
    r = ex.run(v, request_id="r1", payload=None, via_alias=None)
    assert r.state is InvocationState.SUCCEEDED
    assert r.payload == "ok"
    assert ex.inflight(v) == 0
    logger.info("[req=r1] state=%s transitions=%s", r.state.value, r.transitions)


def test_user_error_is_failed_state():
    ex = Executor()

    def boom(_):
        raise ValueError("bad input")

    r = ex.run(_ver(func=boom), request_id="r2", payload=None, via_alias=None)
    assert r.state is InvocationState.FAILED
    assert "ValueError" in r.payload
    assert ex.inflight(_ver()) == 0


def test_concurrency_limit_rejects_without_queueing():
    ex = Executor()
    entered = threading.Event()
    release = threading.Event()

    def blocker(_):
        entered.set()
        release.wait(timeout=5)
        return "done"

    v = _ver(limit=1, func=blocker)
    holder = threading.Thread(target=ex.run, args=(v,),
                              kwargs={"request_id": "hold", "payload": None, "via_alias": None})
    holder.start()
    assert entered.wait(2)

    # 第二个请求必须立即（远小于函数耗时）被拒绝，而不是排队
    t0 = time.time()
    rejected = ex.run(v, request_id="rej", payload=None, via_alias=None)
    elapsed = time.time() - t0
    assert rejected.state is InvocationState.CONCURRENCY_REJECTED
    assert elapsed < 0.2
    assert ex.inflight(v) == 1  # 被拒请求没有占用槽位
    logger.info("[req=rej] state=%s 依据=%s", rejected.state.value, rejected.transitions)

    release.set()
    holder.join(2)
    assert ex.inflight(v) == 0  # 完成后槽位回收，无漂移


def test_timeout_state_and_slot_reaped_after_real_finish():
    ex = Executor()

    def slow(payload):
        time.sleep(0.3)
        return "late"

    v = _ver(timeout=0.05, func=slow)
    t0 = time.time()
    r = ex.run(v, request_id="slow", payload=None, via_alias=None)
    assert time.time() - t0 < 0.2
    assert r.state is InvocationState.TIMED_OUT
    # 超时瞬间用户函数仍在跑，槽位被占住（不会被错误释放给别人导致超卖）
    assert ex.inflight(v) == 1
    logger.info("[req=slow] state=TIMED_OUT transitions=%s", r.transitions)

    # 等孤儿调用真正结束后，回收线程释放槽位
    deadline = time.time() + 3
    while time.time() < deadline and ex.inflight(v) != 0:
        time.sleep(0.02)
    assert ex.inflight(v) == 0


def test_separate_version_counters_do_not_interfere():
    ex = Executor()
    gate = threading.Event()
    v1 = _ver(version="v1", limit=1, func=lambda p: gate.wait(2) or "v1")
    v2 = _ver(version="v2", limit=1, func=lambda p: "v2")

    t = threading.Thread(target=ex.run, args=(v1,),
                         kwargs={"request_id": "a", "payload": None, "via_alias": None})
    t.start()
    time.sleep(0.05)
    # v1 占满，不影响 v2
    r2 = ex.run(v2, request_id="b", payload=None, via_alias=None)
    assert r2.state is InvocationState.SUCCEEDED and r2.payload == "v2"
    # v1 仍满
    r1_extra = ex.run(v1, request_id="c", payload=None, via_alias=None)
    assert r1_extra.state is InvocationState.CONCURRENCY_REJECTED
    gate.set()
    t.join(2)
