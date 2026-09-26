"""Engine semantics: concurrency, timeout, idempotency, lifecycle drift."""

from __future__ import annotations

import logging
import threading
import time

import pytest

from app import Gateway, Target
from app.errors import (
    FunctionDisabledError,
    RequestConflictError,
)
from app.types import InvocationState


@pytest.fixture()
def gateway() -> Gateway:
    gw = Gateway(pool_size=32)
    yield gw
    gw.engine.shutdown(wait=False)


def _wait_terminal(gw: Gateway, rid: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = gw.get_result(rid)
        if rec is not None and rec.state.terminal:
            return rec
        time.sleep(0.005)
    raise AssertionError(f"request {rid} never reached terminal state")


# ---------------------------------------------------------------------------
# Basic outcomes
# ---------------------------------------------------------------------------


def test_sync_success(gateway: Gateway) -> None:
    gateway.register_function("echo", "v1", lambda p: {"got": p})
    rec = gateway.invoke(
        "echo", Target.version("v1"), 7, request_id="req-sync-ok"
    )
    assert rec.state is InvocationState.SUCCEEDED
    assert rec.result == {"got": 7}
    assert rec.error_code is None
    assert rec.resolved_version == "v1"


def test_handler_failure_is_distinct_state(gateway: Gateway) -> None:
    def boom(_):
        raise ValueError("kaboom")

    gateway.register_function("boom", "v1", boom)
    rec = gateway.invoke(
        "boom", Target.version("v1"), None, request_id="req-fail-1"
    )
    assert rec.state is InvocationState.FAILED
    assert rec.error_code == "HANDLER_ERROR"
    assert "kaboom" in (rec.error or "")


# ---------------------------------------------------------------------------
# Request-id dedupe
# ---------------------------------------------------------------------------


def test_duplicate_request_id_converges(gateway: Gateway) -> None:
    gateway.register_function("echo", "v1", lambda p: p)
    first = gateway.invoke(
        "echo", Target.version("v1"), {"k": 1}, request_id="req-dedup-1"
    )
    second = gateway.invoke(
        "echo", Target.version("v1"), {"k": 1}, request_id="req-dedup-1"
    )
    assert second is first
    assert second.state is InvocationState.SUCCEEDED


def test_same_id_different_params_is_conflict(gateway: Gateway) -> None:
    gateway.register_function("echo", "v1", lambda p: p)
    gateway.invoke(
        "echo", Target.version("v1"), {"k": 1}, request_id="req-conflict-1"
    )
    with pytest.raises(RequestConflictError) as exc:
        gateway.invoke(
            "echo",
            Target.version("v1"),
            {"k": 2},
            request_id="req-conflict-1",
        )
    assert exc.value.code == "REQUEST_CONFLICT"


def test_concurrent_duplicate_ids_run_handler_once(
    gateway: Gateway, caplog: pytest.LogCaptureFixture
) -> None:
    runs = 0
    runs_lock = threading.Lock()
    started = threading.Event()

    def handler(_):
        nonlocal runs
        with runs_lock:
            runs += 1
        started.set()
        time.sleep(0.2)
        return "once"

    gateway.register_function(
        "id", "v1", handler, timeout_seconds=2.0, max_concurrency=8
    )

    barrier = threading.Barrier(6)
    outcomes: dict[int, InvocationState] = {}

    def worker(i: int) -> None:
        barrier.wait()
        rec = gateway.invoke(
            "id", Target.version("v1"), "p", request_id="req-race-1"
        )
        outcomes[i] = rec.state

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    with caplog.at_level(logging.INFO, logger="gateway.engine"):
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert runs == 1, "exactly one owner must execute the handler"
    assert set(outcomes.values()) == {InvocationState.SUCCEEDED}
    # Logs must carry the request id and the convergence rationale.
    assert "req-race-1" in caplog.text
    assert (
        "reason=shared_request_id_converged" in caplog.text
        or "reason=request_id_resubmitted" in caplog.text
    )


# ---------------------------------------------------------------------------
# Concurrency cap
# ---------------------------------------------------------------------------


def test_concurrency_cap_throttles_without_queueing(
    gateway: Gateway, caplog: pytest.LogCaptureFixture
) -> None:
    def slow(_):
        time.sleep(0.3)
        return "slow-ok"

    gateway.register_function(
        "slow", "v1", slow, timeout_seconds=2.0, max_concurrency=2
    )

    barrier = threading.Barrier(4)
    records = {}

    def call(i: int) -> None:
        barrier.wait()
        records[i] = gateway.invoke(
            "slow", Target.version("v1"), None, request_id=f"req-cap-{i}"
        )

    threads = [threading.Thread(target=call, args=(i,)) for i in range(4)]
    with caplog.at_level(logging.INFO, logger="gateway.engine"):
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    states = sorted(r.state.value for r in records.values())
    assert states == ["SUCCEEDED", "SUCCEEDED", "THROTTLED", "THROTTLED"]
    throttled = [r for r in records.values() if r.state is
                 InvocationState.THROTTLED]
    assert {r.error_code for r in throttled} == {"THROTTLED"}
    assert gateway.inflight("slow", "v1") == 0
    assert "reason=concurrency_capacity_exceeded" in caplog.text


def test_throttle_state_is_distinct_from_timeout_and_failure(
    gateway: Gateway,
) -> None:
    gateway.register_function(
        "s", "v1", lambda _: time.sleep(0.3),
        timeout_seconds=2.0, max_concurrency=1,
    )
    barrier = threading.Barrier(2)
    results = []

    def call(rid: str) -> None:
        barrier.wait()
        results.append(
            gateway.invoke("s", Target.version("v1"), None, request_id=rid)
        )

    t1 = threading.Thread(target=call, args=("req-dist-1",))
    t2 = threading.Thread(target=call, args=("req-dist-2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    codes = {r.error_code for r in results}
    assert codes == {None, "THROTTLED"}


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_timeout_is_distinct_and_slot_reclaimed(
    gateway: Gateway, caplog: pytest.LogCaptureFixture
) -> None:
    gateway.register_function(
        "hang", "v1", lambda _: time.sleep(1.0),
        timeout_seconds=0.2, max_concurrency=1,
    )
    with caplog.at_level(logging.INFO, logger="gateway.engine"):
        rec = gateway.invoke(
            "hang", Target.version("v1"), None, request_id="req-timeout-1"
        )
    assert rec.state is InvocationState.TIMED_OUT
    assert rec.error_code == "TIMED_OUT"
    # Counters must be reclaimable at the deadline, with no drift.
    assert gateway.inflight("hang", "v1") == 0
    assert "reason=timeout_budget_exhausted" in caplog.text
    assert "req-timeout-1" in caplog.text

    # The slot is immediately reusable even though the abandoned thread may
    # still be running; and a late return cannot rewrite the conclusion.
    follow = gateway.invoke(
        "hang", Target.version("v1"), None, request_id="req-timeout-2"
    )
    assert follow.state is InvocationState.TIMED_OUT
    time.sleep(0.95)
    assert gateway.get_result("req-timeout-1").state is InvocationState.TIMED_OUT
    assert gateway.inflight("hang", "v1") == 0


# ---------------------------------------------------------------------------
# Async
# ---------------------------------------------------------------------------


def test_async_accepted_then_queryable_and_deduped(gateway: Gateway) -> None:
    gateway.register_function("echo", "v1", lambda p: p)
    accepted = gateway.invoke_async(
        "echo", Target.version("v1"), "hi", request_id="req-async-1"
    )
    assert accepted.state in {
        InvocationState.ACCEPTED,
        InvocationState.RUNNING,
    }
    terminal = _wait_terminal(gateway, "req-async-1")
    assert terminal.state is InvocationState.SUCCEEDED
    assert terminal.result == "hi"

    # Duplicate async submission returns the same converged record.
    dup = gateway.invoke_async(
        "echo", Target.version("v1"), "hi", request_id="req-async-1"
    )
    assert dup.state is InvocationState.SUCCEEDED
    assert dup.result == "hi"


def test_async_timeout_queryable(gateway: Gateway) -> None:
    gateway.register_function(
        "hang2", "v1", lambda _: time.sleep(1.0),
        timeout_seconds=0.15, max_concurrency=2,
    )
    gateway.invoke_async(
        "hang2", Target.version("v1"), None, request_id="req-async-to"
    )
    rec = _wait_terminal(gateway, "req-async-to")
    assert rec.state is InvocationState.TIMED_OUT
    assert gateway.inflight("hang2", "v1") == 0


# ---------------------------------------------------------------------------
# Lifecycle: alias switch / disable during in-flight calls
# ---------------------------------------------------------------------------


def test_inflight_call_pinned_across_alias_switch(gateway: Gateway) -> None:
    def slow_v1(_):
        time.sleep(0.3)
        return "from-v1"

    gateway.register_function(
        "svc", "v1", slow_v1, timeout_seconds=2.0, max_concurrency=4
    )
    gateway.register_function(
        "svc", "v2", lambda _: "from-v2",
        timeout_seconds=2.0, max_concurrency=4,
    )
    gateway.set_alias("svc", "live", "v1")

    holder = {}

    def inflight() -> None:
        holder["rec"] = gateway.invoke(
            "svc", Target.alias("live"), None, request_id="req-switch-1"
        )

    t = threading.Thread(target=inflight)
    t.start()
    time.sleep(0.08)
    gateway.set_alias("svc", "live", "v2")
    t.join()

    assert holder["rec"].resolved_version == "v1"
    assert holder["rec"].result == "from-v1"

    after = gateway.invoke(
        "svc", Target.alias("live"), None, request_id="req-switch-2"
    )
    assert after.resolved_version == "v2"
    assert after.result == "from-v2"


def test_disable_does_not_lose_inflight_and_blocks_new(gateway: Gateway) -> None:
    gateway.register_function(
        "svc2", "v1", lambda _: time.sleep(0.3) or "ok",
        timeout_seconds=2.0, max_concurrency=4,
    )
    holder = {}

    def inflight() -> None:
        holder["rec"] = gateway.invoke(
            "svc2", Target.version("v1"), None, request_id="req-stop-1"
        )

    t = threading.Thread(target=inflight)
    t.start()
    time.sleep(0.08)
    gateway.disable("svc2")

    with pytest.raises(FunctionDisabledError) as exc:
        gateway.invoke(
            "svc2", Target.version("v1"), None, request_id="req-stop-2"
        )
    assert exc.value.code == "FUNCTION_DISABLED"

    t.join()
    assert holder["rec"].state is InvocationState.SUCCEEDED
    assert holder["rec"].result == "ok"
    assert gateway.inflight("svc2", "v1") == 0

    gateway.enable("svc2")
    again = gateway.invoke(
        "svc2", Target.version("v1"), None, request_id="req-stop-3"
    )
    assert again.state is InvocationState.SUCCEEDED


def test_concurrency_counters_have_no_drift_after_mixed_load(
    gateway: Gateway,
) -> None:
    counter = {"n": 0}
    lock = threading.Lock()

    def handler(_):
        with lock:
            counter["n"] += 1
        time.sleep(0.05)
        if counter["n"] % 3 == 0:
            raise RuntimeError("periodic failure")
        return counter["n"]

    gateway.register_function(
        "mix", "v1", handler, timeout_seconds=2.0, max_concurrency=3
    )

    barrier = threading.Barrier(12)
    states: list[InvocationState] = []
    slock = threading.Lock()

    def call(i: int) -> None:
        barrier.wait()
        rec = gateway.invoke(
            "mix", Target.version("v1"), None, request_id=f"req-mix-{i}"
        )
        with slock:
            states.append(rec.state)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert gateway.inflight("mix", "v1") == 0
    assert set(states) <= {
        InvocationState.SUCCEEDED,
        InvocationState.FAILED,
        InvocationState.THROTTLED,
    }
    # A final call must still get a slot -> no counter drift.
    final = gateway.invoke(
        "mix", Target.version("v1"), None, request_id="req-mix-final"
    )
    assert final.state is InvocationState.SUCCEEDED
