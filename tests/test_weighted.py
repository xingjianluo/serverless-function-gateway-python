"""Weighted (grey-rollout) routing: distribution, validation, smooth shifts.

Covered behaviours (each test logs the gateway transitions it triggers):
* traffic splits across versions proportionally to integer weights;
* invalid weight configs are rejected with distinguishable reason codes;
* weight changes apply to new calls immediately, 0-weight drains a version;
* in-flight calls stay pinned to their admission-time version and their
  outcome is accounted to that version only;
* per-version concurrency caps hold under timeouts/failures, and the
  inflight counters never drift;
* duplicate request ids still converge to a single conclusion;
* legacy single-version aliases and direct version targets are unaffected.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import Gateway, Target
from app.demo import build_app
from app.errors import (
    InvalidRequestError,
    RoutingEmptyError,
    VersionNotFoundError,
    WeightNegativeError,
    WeightSumInvalidError,
)
from app.types import InvocationState


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


def _register_two(gw: Gateway, name: str = "svc") -> None:
    gw.register_function(name, "v1", lambda p: f"v1:{p}")
    gw.register_function(name, "v2", lambda p: f"v2:{p}")


# ---------------------------------------------------------------------------
# Proportional distribution
# ---------------------------------------------------------------------------


def test_traffic_splits_proportionally_to_weights(
    gateway: Gateway, caplog: pytest.LogCaptureFixture
) -> None:
    _register_two(gateway)
    gateway.set_alias_weights("svc", "live", {"v1": 75, "v2": 25})

    total = 800
    counts = {"v1": 0, "v2": 0}
    with caplog.at_level(logging.INFO, logger="gateway.registry"):
        for i in range(total):
            rec = gateway.invoke(
                "svc", Target.alias("live"), i, request_id=f"dist-{i}"
            )
            assert rec.state is InvocationState.SUCCEEDED
            counts[rec.resolved_version] += 1
            assert rec.result == f"{rec.resolved_version}:{i}"

    ratio = counts["v1"] / total
    # 75/25 split: 4.5-sigma band around the expectation (std ~= 12.2).
    assert 0.68 <= ratio <= 0.82, f"observed split {counts}"
    # Per-version accounting adds up to the total number of calls.
    stats = gateway.function_overview("svc")["versions"]
    assert stats["v1"]["terminal"]["SUCCEEDED"] == counts["v1"]
    assert stats["v2"]["terminal"]["SUCCEEDED"] == counts["v2"]
    assert "reason=weighted_alias_resolved" in caplog.text


def test_weight_change_applies_immediately_and_zero_drains(
    gateway: Gateway,
) -> None:
    _register_two(gateway)
    gateway.set_alias_weights("svc", "live", {"v1": 100})
    first = gateway.invoke("svc", Target.alias("live"), None, request_id="w-1")
    assert first.resolved_version == "v1"

    # Shift all traffic to v2 without restarting anything.
    gateway.set_alias_weights("svc", "live", {"v1": 0, "v2": 100})
    for i in range(50):
        rec = gateway.invoke(
            "svc", Target.alias("live"), None, request_id=f"w-shift-{i}"
        )
        assert rec.resolved_version == "v2"

    overview = gateway.function_overview("svc")
    assert overview["aliases"]["live"] == {"v1": 0, "v2": 100}
    # v1 keeps its historic counters; nothing new was attributed to it.
    assert overview["versions"]["v1"]["terminal"]["SUCCEEDED"] == 1
    assert overview["versions"]["v2"]["terminal"]["SUCCEEDED"] == 50


# ---------------------------------------------------------------------------
# Validation: rejections are distinguishable
# ---------------------------------------------------------------------------


def test_empty_routing_rejected_distinctly(gateway: Gateway) -> None:
    _register_two(gateway)
    with pytest.raises(RoutingEmptyError) as exc:
        gateway.set_alias_weights("svc", "live", {})
    assert exc.value.code == "ROUTING_EMPTY"


def test_weight_sum_must_be_exactly_100(gateway: Gateway) -> None:
    _register_two(gateway)
    for bad in ({"v1": 50, "v2": 40}, {"v1": 60, "v2": 60}, {"v1": 99}):
        with pytest.raises(WeightSumInvalidError) as exc:
            gateway.set_alias_weights("svc", "live", bad)
        assert exc.value.code == "WEIGHT_SUM_INVALID"


def test_negative_weight_rejected_distinctly(gateway: Gateway) -> None:
    _register_two(gateway)
    with pytest.raises(WeightNegativeError) as exc:
        gateway.set_alias_weights("svc", "live", {"v1": -10, "v2": 110})
    assert exc.value.code == "WEIGHT_NEGATIVE"


def test_unpublished_version_rejected_distinctly(gateway: Gateway) -> None:
    _register_two(gateway)
    with pytest.raises(VersionNotFoundError) as exc:
        gateway.set_alias_weights("svc", "live", {"v1": 60, "v9": 40})
    assert exc.value.code == "VERSION_NOT_FOUND"


def test_non_integer_weight_rejected(gateway: Gateway) -> None:
    _register_two(gateway)
    for bad in ({"v1": 50.5, "v2": 49.5}, {"v1": True, "v2": 99}):
        with pytest.raises(InvalidRequestError) as exc:
            gateway.set_alias_weights("svc", "live", bad)
        assert exc.value.code == "INVALID_REQUEST"


def test_rejected_config_leaves_previous_weights_untouched(
    gateway: Gateway,
) -> None:
    _register_two(gateway)
    gateway.set_alias_weights("svc", "live", {"v1": 100})
    with pytest.raises(WeightSumInvalidError):
        gateway.set_alias_weights("svc", "live", {"v1": 10, "v2": 10})
    rec = gateway.invoke("svc", Target.alias("live"), None, request_id="keep-1")
    assert rec.resolved_version == "v1"
    assert gateway.registry.get_alias_weights("svc", "live") == {"v1": 100}


# ---------------------------------------------------------------------------
# Smooth shifting: in-flight calls are pinned and accounted to their version
# ---------------------------------------------------------------------------


def test_inflight_call_pinned_and_accounted_during_weight_shift(
    gateway: Gateway,
) -> None:
    release = threading.Event()
    started = threading.Event()

    def slow_v1(_):
        started.set()
        release.wait(timeout=5.0)
        return "v1-done"

    gateway.register_function("svc", "v1", slow_v1, timeout_seconds=5.0)
    gateway.register_function("svc", "v2", lambda p: "v2-done")
    gateway.set_alias_weights("svc", "live", {"v1": 100})

    accepted = gateway.invoke_async(
        "svc", Target.alias("live"), None, request_id="inflight-1"
    )
    assert accepted.resolved_version == "v1"
    assert started.wait(timeout=2.0)

    # Shift 100% to v2 while the v1 call is still running.
    gateway.set_alias_weights("svc", "live", {"v1": 0, "v2": 100})
    new_call = gateway.invoke(
        "svc", Target.alias("live"), None, request_id="after-shift"
    )
    assert new_call.resolved_version == "v2"

    release.set()
    finished = _wait_terminal(gateway, "inflight-1")
    assert finished.state is InvocationState.SUCCEEDED
    assert finished.resolved_version == "v1"
    assert finished.result == "v1-done"

    overview = gateway.function_overview("svc")
    # The in-flight result is accounted to v1, not to v2.
    assert overview["versions"]["v1"]["terminal"]["SUCCEEDED"] == 1
    assert overview["versions"]["v2"]["terminal"]["SUCCEEDED"] == 1
    assert overview["versions"]["v1"]["inflight"] == 0
    assert overview["versions"]["v2"]["inflight"] == 0


# ---------------------------------------------------------------------------
# Concurrency caps stay true under abnormal endings; counters never drift
# ---------------------------------------------------------------------------


def test_concurrency_cap_holds_under_timeouts_and_no_drift(
    gateway: Gateway,
) -> None:
    # v1 always times out (handler outlives its deadline); v2 succeeds.
    gateway.register_function(
        "svc",
        "v1",
        lambda p: time.sleep(0.6),
        timeout_seconds=0.15,
        max_concurrency=2,
    )
    gateway.register_function(
        "svc",
        "v2",
        lambda p: "ok",
        timeout_seconds=2.0,
        max_concurrency=3,
    )
    gateway.set_alias_weights("svc", "live", {"v1": 50, "v2": 50})

    total = 60
    stop = threading.Event()
    observed = {"v1": 0, "v2": 0}

    def monitor() -> None:
        while not stop.is_set():
            for v in ("v1", "v2"):
                observed[v] = max(observed[v], gateway.inflight("svc", v))
            time.sleep(0.001)

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()

    def call(i: int) -> None:
        gateway.invoke("svc", Target.alias("live"), None, request_id=f"mix-{i}")

    threads = [threading.Thread(target=call, args=(i,)) for i in range(total)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    watcher.join()

    # The engine's own accounting never exceeded a version's cap, even
    # though v1 calls ended in TIMED_OUT while their workers kept running.
    assert observed["v1"] <= 2, f"v1 inflight peaked at {observed['v1']}"
    assert observed["v2"] <= 3, f"v2 inflight peaked at {observed['v2']}"

    overview = gateway.function_overview("svc")["versions"]
    # No drift: everything reclaimed, no residue left behind.
    assert overview["v1"]["inflight"] == 0
    assert overview["v2"]["inflight"] == 0
    concluded = sum(
        sum(overview[v]["terminal"].values()) for v in ("v1", "v2")
    )
    assert concluded == total
    # v1 could only end TIMED_OUT or THROTTLED, never SUCCEEDED.
    assert set(overview["v1"]["terminal"]) <= {"TIMED_OUT", "THROTTLED"}
    assert overview["v1"]["terminal"].get("TIMED_OUT", 0) >= 1


def test_terminal_counters_match_admissions_across_mixed_outcomes(
    gateway: Gateway,
) -> None:
    gateway.register_function("svc", "v1", lambda p: "ok", max_concurrency=2)
    gateway.register_function(
        "svc",
        "v2",
        lambda p: (_ for _ in ()).throw(RuntimeError("boom")),
        max_concurrency=2,
    )
    gateway.set_alias_weights("svc", "live", {"v1": 50, "v2": 50})

    total = 120
    for i in range(total):
        rec = gateway.invoke(
            "svc", Target.alias("live"), None, request_id=f"acct-{i}"
        )
        assert rec.state in (
            InvocationState.SUCCEEDED,
            InvocationState.FAILED,
            InvocationState.THROTTLED,
        )

    overview = gateway.function_overview("svc")["versions"]
    assert overview["v1"]["inflight"] == 0
    assert overview["v2"]["inflight"] == 0
    assert overview["v1"]["terminal"] == {
        "SUCCEEDED": sum(overview["v1"]["terminal"].values())
    }
    assert set(overview["v2"]["terminal"]) <= {"FAILED", "THROTTLED"}
    concluded = sum(
        sum(overview[v]["terminal"].values()) for v in ("v1", "v2")
    )
    assert concluded == total


# ---------------------------------------------------------------------------
# Idempotency across the weighted alias
# ---------------------------------------------------------------------------


def test_duplicate_request_id_converges_across_weighted_alias(
    gateway: Gateway,
) -> None:
    runs = 0
    runs_lock = threading.Lock()

    def handler(_):
        nonlocal runs
        with runs_lock:
            runs += 1
        time.sleep(0.2)
        return "once"

    gateway.register_function("svc", "v1", handler, max_concurrency=8)
    gateway.register_function("svc", "v2", handler, max_concurrency=8)
    gateway.set_alias_weights("svc", "live", {"v1": 50, "v2": 50})

    barrier = threading.Barrier(8)
    records = {}

    def worker(i: int) -> None:
        barrier.wait()
        records[i] = gateway.invoke(
            "svc", Target.alias("live"), "p", request_id="dedup-weighted"
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert runs == 1, "the handler must run exactly once"
    versions = {r.resolved_version for r in records.values()}
    assert len(versions) == 1, "all duplicates converge to one version"
    assert {r.state for r in records.values()} == {InvocationState.SUCCEEDED}
    overview = gateway.function_overview("svc")["versions"]
    winner = versions.pop()
    assert overview[winner]["terminal"]["SUCCEEDED"] == 1


# ---------------------------------------------------------------------------
# Backwards compatibility
# ---------------------------------------------------------------------------


def test_single_version_alias_and_direct_version_unaffected(
    gateway: Gateway,
) -> None:
    _register_two(gateway)
    gateway.set_alias("svc", "stable", "v1")  # legacy single-version API
    for i in range(20):
        rec = gateway.invoke(
            "svc", Target.alias("stable"), None, request_id=f"legacy-{i}"
        )
        assert rec.resolved_version == "v1"
    assert gateway.registry.get_alias("svc", "stable") == "v1"
    assert gateway.registry.get_alias_weights("svc", "stable") == {"v1": 100}
    assert gateway.registry.list_aliases("svc") == {"stable": "v1"}

    direct = gateway.invoke(
        "svc", Target.version("v2"), None, request_id="direct-1"
    )
    assert direct.resolved_version == "v2"
    assert direct.result == "v2:None"


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    with TestClient(build_app()) as c:
        yield c


def test_http_weighted_alias_and_overview(client: TestClient) -> None:
    r = client.put(
        "/admin/functions/echo/aliases/current",
        json={"alias": "current", "weights": {"v1": 50, "v2": 50}},
    )
    assert r.status_code == 200
    assert r.json()["weights"] == {"v1": 50, "v2": 50}

    seen = set()
    for i in range(60):
        resp = client.post(
            "/functions/echo/aliases/current/invoke",
            json={"request_id": f"http-w-{i}", "payload": i},
        )
        assert resp.status_code == 200
        seen.add(resp.json()["resolved_version"])
    assert seen == {"v1", "v2"}

    overview = client.get("/functions/echo/overview").json()
    assert overview["aliases"]["current"] == {"v1": 50, "v2": 50}
    assert overview["versions"]["v1"]["max_concurrency"] == 8
    concluded = sum(
        sum(overview["versions"][v]["terminal"].values()) for v in ("v1", "v2")
    )
    assert concluded == 60

    # restore the demo default for other tests
    client.put(
        "/admin/functions/echo/aliases/current",
        json={"alias": "current", "version": "v1"},
    )


def test_http_invalid_weights_map_to_400_with_distinct_codes(
    client: TestClient,
) -> None:
    cases = [
        ({"v1": 50, "v2": 40}, "WEIGHT_SUM_INVALID"),
        ({"v1": -5, "v2": 105}, "WEIGHT_NEGATIVE"),
        ({"v1": 50, "ghost": 50}, "VERSION_NOT_FOUND"),
        ({}, "ROUTING_EMPTY"),
    ]
    for weights, code in cases:
        r = client.put(
            "/admin/functions/echo/aliases/current",
            json={"alias": "current", "weights": weights},
        )
        assert r.status_code in (400, 404), r.text
        assert r.json()["error_code"] == code
