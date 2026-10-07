"""Weighted-alias (gray rollout) semantics.

Covers: proportional traffic split, distinct rejection codes for invalid
weights, in-flight pinning across weight changes, zero-weight draining,
per-version concurrency truth under timeouts, counter drift-freedom and
request-id convergence across routing styles.
"""

from __future__ import annotations

import threading
import time
from collections import Counter

import pytest

from app import Gateway, InvocationState, Target
from app.errors import (
    AliasWeightsEmptyError,
    InvalidRequestError,
    InvalidWeightError,
    RequestConflictError,
    VersionNotFoundError,
    WeightSumInvalidError,
)


@pytest.fixture()
def gateway() -> Gateway:
    gw = Gateway(pool_size=64)
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


def _stats(gw: Gateway, name: str) -> dict:
    return {s.version: s for s in gw.version_stats(name)}


# ---------------------------------------------------------------------------
# Proportional distribution
# ---------------------------------------------------------------------------


def test_weighted_split_matches_configured_proportions(
    gateway: Gateway,
) -> None:
    gateway.register_function("dist", "v1", lambda p: "v1")
    gateway.register_function("dist", "v2", lambda p: "v2")
    gateway.set_alias_weights("dist", "live", {"v1": 70, "v2": 30})

    counts: Counter[str] = Counter()
    for i in range(200):
        rec = gateway.invoke(
            "dist", Target.alias("live"), None, request_id=f"dist-a-{i}"
        )
        assert rec.state is InvocationState.SUCCEEDED
        counts[rec.resolved_version] += 1
    assert counts == {"v1": 140, "v2": 60}

    # Online re-weighting takes effect on the very next call, no restart.
    gateway.set_alias_weights("dist", "live", {"v1": 25, "v2": 75})
    counts.clear()
    for i in range(200):
        rec = gateway.invoke(
            "dist", Target.alias("live"), None, request_id=f"dist-b-{i}"
        )
        counts[rec.resolved_version] += 1
    assert counts == {"v1": 50, "v2": 150}


def test_three_way_split_and_result_attribution(gateway: Gateway) -> None:
    gateway.register_function("tri", "v1", lambda p: "r1")
    gateway.register_function("tri", "v2", lambda p: "r2")
    gateway.register_function("tri", "v3", lambda p: "r3")
    gateway.set_alias_weights(
        "tri", "live", {"v1": 50, "v2": 25, "v3": 25}
    )
    counts: Counter[str] = Counter()
    for i in range(100):
        rec = gateway.invoke(
            "tri", Target.alias("live"), None, request_id=f"tri-{i}"
        )
        # The result comes from the version the record is attributed to.
        assert rec.result == {"v1": "r1", "v2": "r2", "v3": "r3"}[
            rec.resolved_version
        ]
        counts[rec.resolved_version] += 1
    assert counts == {"v1": 50, "v2": 25, "v3": 25}
    stats = _stats(gateway, "tri")
    assert stats["v1"].succeeded == 50
    assert stats["v2"].succeeded == 25
    assert stats["v3"].succeeded == 25


# ---------------------------------------------------------------------------
# Invalid configurations are rejected with distinguishable codes
# ---------------------------------------------------------------------------


def test_invalid_weight_configs_have_distinct_codes(gateway: Gateway) -> None:
    gateway.register_function("bad", "v1", lambda p: p)
    gateway.register_function("bad", "v2", lambda p: p)

    with pytest.raises(AliasWeightsEmptyError) as exc:
        gateway.set_alias_weights("bad", "live", {})
    assert exc.value.code == "ALIAS_WEIGHTS_EMPTY"

    with pytest.raises(InvalidWeightError) as exc:
        gateway.set_alias_weights("bad", "live", {"v1": -10, "v2": 110})
    assert exc.value.code == "INVALID_WEIGHT"

    with pytest.raises(InvalidWeightError) as exc:
        gateway.set_alias_weights("bad", "live", {"v1": 50.5, "v2": 49.5})
    assert exc.value.code == "INVALID_WEIGHT"

    with pytest.raises(WeightSumInvalidError) as exc:
        gateway.set_alias_weights("bad", "live", {"v1": 60, "v2": 60})
    assert exc.value.code == "WEIGHT_SUM_INVALID"

    with pytest.raises(VersionNotFoundError) as exc:
        gateway.set_alias_weights("bad", "live", {"v1": 50, "v9": 50})
    assert exc.value.code == "VERSION_NOT_FOUND"

    with pytest.raises(InvalidRequestError) as exc:
        gateway.set_alias_weights("bad", "live", [("v1", 100)])  # type: ignore[arg-type]
    assert exc.value.code == "INVALID_REQUEST"


def test_rejected_config_leaves_previous_routing_untouched(
    gateway: Gateway,
) -> None:
    gateway.register_function("keep", "v1", lambda p: "v1")
    gateway.register_function("keep", "v2", lambda p: "v2")
    gateway.set_alias("keep", "live", "v1")

    with pytest.raises(WeightSumInvalidError):
        gateway.set_alias_weights("keep", "live", {"v1": 40, "v2": 40})

    assert gateway.get_alias_weights("keep", "live") == {"v1": 100}
    for i in range(10):
        rec = gateway.invoke(
            "keep", Target.alias("live"), None, request_id=f"keep-{i}"
        )
        assert rec.resolved_version == "v1"


# ---------------------------------------------------------------------------
# Smooth re-weighting: in-flight calls are pinned and correctly attributed
# ---------------------------------------------------------------------------


def test_inflight_call_pinned_and_attributed_across_reweight(
    gateway: Gateway,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def v1_handler(_):
        started.set()
        release.wait(timeout=5)
        return "from-v1"

    gateway.register_function("svc", "v1", v1_handler, timeout_seconds=5.0)
    gateway.register_function("svc", "v2", lambda p: "from-v2")
    gateway.set_alias("svc", "live", "v1")

    accepted = gateway.invoke_async(
        "svc", Target.alias("live"), None, request_id="w-inflight"
    )
    assert accepted.state is InvocationState.ACCEPTED
    assert started.wait(timeout=2)

    # Re-weight while the call is running: v1 drains to 0%.
    gateway.set_alias_weights("svc", "live", {"v1": 0, "v2": 100})

    # New calls immediately follow the new weights.
    rec_new = gateway.invoke(
        "svc", Target.alias("live"), None, request_id="w-after-switch"
    )
    assert rec_new.resolved_version == "v2"
    assert rec_new.result == "from-v2"

    # The in-flight call finishes on v1 and is booked to v1.
    release.set()
    final = _wait_terminal(gateway, "w-inflight")
    assert final.state is InvocationState.SUCCEEDED
    assert final.resolved_version == "v1"
    assert final.result == "from-v1"

    stats = _stats(gateway, "svc")
    assert stats["v1"].succeeded == 1
    assert stats["v2"].succeeded == 1
    assert stats["v1"].inflight == 0


def test_zero_weight_version_receives_no_new_traffic(gateway: Gateway) -> None:
    gateway.register_function("zero", "v1", lambda p: "v1")
    gateway.register_function("zero", "v2", lambda p: "v2")
    gateway.set_alias_weights("zero", "live", {"v1": 100, "v2": 0})

    for i in range(50):
        rec = gateway.invoke(
            "zero", Target.alias("live"), None, request_id=f"zero-{i}"
        )
        assert rec.resolved_version == "v1"

    stats = _stats(gateway, "zero")
    assert stats["v2"].succeeded == 0
    assert stats["v2"].inflight == 0
    # A drained version is observably idle: safe to decommission.
    assert stats["v1"].succeeded == 50


# ---------------------------------------------------------------------------
# Per-version concurrency ceilings stay true under abnormal endings
# ---------------------------------------------------------------------------


def test_concurrency_ceiling_holds_through_timeouts(gateway: Gateway) -> None:
    def slow(_):
        time.sleep(1.0)
        return "late"

    gateway.register_function(
        "cap", "v1", slow, timeout_seconds=0.3, max_concurrency=2
    )
    gateway.register_function("cap", "v2", lambda p: "fast")
    gateway.set_alias_weights("cap", "live", {"v1": 100, "v2": 0})

    peak = 0
    stop = threading.Event()
    results: list = []

    def watcher() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, gateway.inflight("cap", "v1"))
            time.sleep(0.005)

    def call(i: int) -> None:
        results.append(
            gateway.invoke(
                "cap", Target.alias("live"), None, request_id=f"cap-{i}"
            )
        )

    watch = threading.Thread(target=watcher)
    watch.start()
    threads = [threading.Thread(target=call, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    watch.join()

    # The ceiling was never exceeded, even though every admitted call
    # ended abnormally (timeout) or was refused (throttle).
    assert peak <= 2
    states = Counter(r.state for r in results)
    assert states[InvocationState.TIMED_OUT] == 2
    assert states[InvocationState.THROTTLED] == 4

    # Wait past the late worker returns: nothing leaks, nothing drifts.
    time.sleep(1.2)
    assert gateway.inflight("cap", "v1") == 0
    stats = _stats(gateway, "cap")
    assert stats["v1"].timed_out == 2
    assert stats["v1"].throttled == 4
    assert stats["v1"].inflight == 0


def test_no_counter_drift_after_mixed_load(gateway: Gateway) -> None:
    def v1_handler(payload):
        if payload == "boom":
            raise RuntimeError("v1 boom")
        return "v1-ok"

    def v2_handler(payload):
        if payload == "slow":
            time.sleep(0.5)  # times out (budget 0.1s), returns late
            return "v2-late"
        return "v2-ok"

    gateway.register_function(
        "mix", "v1", v1_handler, timeout_seconds=2.0, max_concurrency=3
    )
    gateway.register_function(
        "mix", "v2", v2_handler, timeout_seconds=0.1, max_concurrency=3
    )
    gateway.set_alias_weights("mix", "live", {"v1": 50, "v2": 50})

    payloads = ["ok", "boom", "ok", "slow"]  # per-version outcomes vary
    rids = []
    for i in range(40):
        rid = f"mix-{i}"
        gateway.invoke_async(
            "mix",
            Target.alias("live"),
            payloads[i % len(payloads)],
            request_id=rid,
        )
        rids.append(rid)

    records = [_wait_terminal(gateway, rid) for rid in rids]
    # Let every late worker finish before checking the books.
    time.sleep(0.8)

    stats = _stats(gateway, "mix")
    per_version: Counter[str] = Counter(r.resolved_version for r in records)
    per_state: Counter[tuple[str, InvocationState]] = Counter(
        (r.resolved_version, r.state) for r in records
    )
    for version in ("v1", "v2"):
        s = stats[version]
        assert s.inflight == 0
        total = s.succeeded + s.failed + s.timed_out + s.throttled
        assert total == per_version[version]
        assert s.succeeded == per_state[(version, InvocationState.SUCCEEDED)]
        assert s.failed == per_state[(version, InvocationState.FAILED)]
        assert s.timed_out == per_state[(version, InvocationState.TIMED_OUT)]
        assert s.throttled == per_state[(version, InvocationState.THROTTLED)]
    # And the books stay balanced a moment later: no residual reclamation.
    assert gateway.inflight("mix", "v1") == 0
    assert gateway.inflight("mix", "v2") == 0


# ---------------------------------------------------------------------------
# Request-id convergence across routing styles
# ---------------------------------------------------------------------------


def test_duplicate_id_converges_through_weighted_alias(
    gateway: Gateway,
) -> None:
    calls = 0
    lock = threading.Lock()

    def handler(p):
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.05)
        return p

    gateway.register_function("dedup", "v1", handler)
    gateway.register_function("dedup", "v2", lambda p: p)
    gateway.set_alias_weights("dedup", "live", {"v1": 50, "v2": 50})

    barrier = threading.Barrier(2)
    records: list = []

    def submit() -> None:
        barrier.wait()
        records.append(
            gateway.invoke(
                "dedup",
                Target.alias("live"),
                {"k": 1},
                request_id="dedup-shared",
            )
        )

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert records[0] is records[1]
    assert records[0].state is InvocationState.SUCCEEDED
    assert calls == 1  # handler ran exactly once


def test_same_id_via_alias_and_direct_version_cannot_fork(
    gateway: Gateway,
) -> None:
    gateway.register_function("conv", "v1", lambda p: "v1")
    gateway.register_function("conv", "v2", lambda p: "v2")
    gateway.set_alias_weights("conv", "live", {"v1": 100, "v2": 0})

    first = gateway.invoke(
        "conv", Target.alias("live"), {"k": 1}, request_id="conv-1"
    )
    assert first.resolved_version == "v1"

    # The same id through a different routing style is a conflict, and
    # the original conclusion stays the single source of truth.
    with pytest.raises(RequestConflictError) as exc:
        gateway.invoke(
            "conv", Target.version("v1"), {"k": 1}, request_id="conv-1"
        )
    assert exc.value.code == "REQUEST_CONFLICT"
    assert gateway.get_result("conv-1") is first

    # The same id with identical parameters converges to the same record.
    again = gateway.invoke(
        "conv", Target.alias("live"), {"k": 1}, request_id="conv-1"
    )
    assert again is first


# ---------------------------------------------------------------------------
# Backward compatibility of the classic routing styles
# ---------------------------------------------------------------------------


def test_single_version_alias_and_direct_invoke_unchanged(
    gateway: Gateway,
) -> None:
    gateway.register_function("compat", "v1", lambda p: "v1")
    gateway.register_function("compat", "v2", lambda p: "v2")
    gateway.set_alias("compat", "current", "v1")

    assert gateway.registry.get_alias("compat", "current") == "v1"
    assert gateway.get_alias_weights("compat", "current") == {"v1": 100}

    for i in range(20):
        rec = gateway.invoke(
            "compat", Target.alias("current"), None, request_id=f"cp-a-{i}"
        )
        assert rec.resolved_version == "v1"

    # Direct version invocation bypasses alias weights entirely.
    rec = gateway.invoke(
        "compat", Target.version("v2"), None, request_id="cp-direct"
    )
    assert rec.resolved_version == "v2" and rec.result == "v2"

    stats = _stats(gateway, "compat")
    assert stats["v1"].succeeded == 20
    assert stats["v2"].succeeded == 1
