"""请求标识幂等收敛、同步/异步、下线与别名切换下的在途隔离。"""

import logging
import threading
import time

import pytest

from gateway.errors import UnknownRequestError
from gateway.invocation import Gateway
from gateway.models import InvocationState

logger = logging.getLogger("gateway.test_gateway")


def _make_gateway(**kwargs):
    g = Gateway()
    g.registry.register("f", "v1", kwargs.get("v1", lambda p: "v1-result"),
                        timeout_seconds=kwargs.get("timeout", 5.0),
                        concurrency_limit=kwargs.get("limit", 4))
    return g


def test_sync_success_and_reproducible_result():
    g = _make_gateway()
    r = g.invoke_sync("f", "v1", None, request_id="req-1")
    assert r.state is InvocationState.SUCCEEDED
    assert r.payload == "v1-result"
    # 重复提交同一 request_id -> 同一结论（不会再执行一次）
    r2 = g.invoke_sync("f", "v1", "ignored-different-payload", request_id="req-1")
    assert r2 is r
    logger.info("[req=req-1] 收敛结论 state=%s target=%s", r2.state.value, r2.target)


def test_concurrent_duplicate_request_converges_single_execution():
    counter = {"n": 0}
    cLock = threading.Lock()

    def counted(_):
        with cLock:
            counter["n"] += 1
        time.sleep(0.15)
        return "same"

    g = Gateway()
    g.registry.register("f", "v1", counted, concurrency_limit=8)

    results: list = []
    barrier = threading.Barrier(8)

    def hit():
        barrier.wait()
        results.append(g.invoke_sync("f", "v1", None, request_id="dup-1"))

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert counter["n"] == 1  # 只有一个提交者真正执行
    states = {r.state for r in results}
    payloads = {r.payload for r in results}
    assert states == {InvocationState.SUCCEEDED}
    assert payloads == {"same"}
    logger.info("[req=dup-1] 8 个并发重复提交收敛为同一结论, 实际执行次数=1")


def test_async_query_converges_and_dedups():
    g = Gateway()
    g.registry.register("f", "v1",
                        lambda p: time.sleep(0.2) or "async-ok",
                        concurrency_limit=2)

    r1 = g.invoke_async("f", "v1", None, request_id="async-1")
    assert r1.state is InvocationState.RUNNING
    # 并发重复的异步提交必须复用同一执行（同一对象），不会重复触发
    r2 = g.invoke_async("f", "v1", "other", request_id="async-1")
    assert r2 is r1

    deadline = time.time() + 3
    while time.time() < deadline and g.query("async-1").state is InvocationState.RUNNING:
        time.sleep(0.01)
    final = g.query("async-1")
    assert final.state is InvocationState.SUCCEEDED and final.payload == "async-ok"

    with pytest.raises(UnknownRequestError) as ei:
        g.query("never")
    assert ei.value.code == "unknown_request"
    logger.info("[req=async-1] RUNNING->SUCCEEDED transitions=%s", final.transitions)


def test_resolution_rejections_are_distinguishable_states():
    g = _make_gateway()
    r_no_ver = g.invoke_sync("f", "v9", None, request_id="x1")
    assert r_no_ver.state is InvocationState.REJECTED_NO_VERSION
    assert "version_not_found" in r_no_ver.payload

    r_no_fn = g.invoke_sync("ghost", "v1", None, request_id="x2")
    assert r_no_fn.state is InvocationState.REJECTED_NO_VERSION
    assert "function_not_found" in r_no_fn.payload

    g.registry.disable("f")
    r_off = g.invoke_sync("f", "v1", None, request_id="x3")
    assert r_off.state is InvocationState.REJECTED_DISABLED
    assert "function_disabled" in r_off.payload
    logger.info("[req=x3] 停用拒绝 state=%s", r_off.state.value)


def test_disable_during_inflight_does_not_lose_or_mix_result():
    """在途调用已绑定不可变 v1；执行期间下线并注册 v2，结果仍是 v1。"""
    started = threading.Event()
    release = threading.Event()

    def v1_func(_):
        started.set()
        release.wait(5)
        return "v1-inflight"

    g = Gateway()
    g.registry.register("f", "v1", v1_func, timeout_seconds=5, concurrency_limit=2)
    g.registry.register("f", "v2", lambda p: "v2-result",
                        timeout_seconds=5, concurrency_limit=2)

    inflight = g.invoke_async("f", "v1", None, request_id="inflight-1")
    assert started.wait(2)

    # 下线 + 新版本发布期间，在途调用继续跑原版本
    g.registry.disable("f")
    # 新请求被拒绝
    rejected = g.invoke_sync("f", "v2", None, request_id="new-while-off")
    assert rejected.state is InvocationState.REJECTED_DISABLED
    g.registry.enable("f")

    release.set()
    deadline = time.time() + 3
    while time.time() < deadline and g.query("inflight-1").state is InvocationState.RUNNING:
        time.sleep(0.01)

    done = g.query("inflight-1")
    assert done.state is InvocationState.SUCCEEDED
    assert done.target == "v1"          # 没有串到 v2
    assert done.payload == "v1-inflight"  # 结果未丢失

    # 结束后槽位回收
    v2 = g.registry.resolve("f", "v2")[0]
    assert g.executor.inflight(v2) == 0
    v1 = g.registry.resolve("f", "v1")[0]
    assert g.executor.inflight(v1) == 0
    logger.info("[req=inflight-1] 在途穿越下线/发布, target=v1 state=SUCCEEDED")


def test_alias_switch_during_inflight_pins_old_version():
    """别名切换只影响之后的新请求，在途调用固定在切换前解析到的版本。"""
    started = threading.Event()
    release = threading.Event()

    g = Gateway()
    g.registry.register("f", "v1", lambda p: (started.set(), release.wait(5), "v1")[2],
                        timeout_seconds=5, concurrency_limit=4)
    g.registry.register("f", "v2", lambda p: "v2",
                        timeout_seconds=5, concurrency_limit=4)
    g.registry.set_alias("f", "prod", "v1")

    old = g.invoke_async("f", "prod", None, request_id="alias-old")
    assert started.wait(2)

    g.registry.set_alias("f", "prod", "v2")  # 在途期间切换别名
    new = g.invoke_sync("f", "prod", None, request_id="alias-new")
    assert new.state is InvocationState.SUCCEEDED
    assert new.target == "v2" and new.via_alias == "prod"

    release.set()
    deadline = time.time() + 3
    while time.time() < deadline and g.query("alias-old").state is InvocationState.RUNNING:
        time.sleep(0.01)
    old_done = g.query("alias-old")
    assert old_done.target == "v1" and old_done.via_alias == "prod"
    assert old_done.payload == "v1"
    logger.info("[req=alias-old] 别名在途切换, 在途固定 v1; [req=alias-new] 新请求走 v2")
