"""HTTP protocol tests: status mapping, async polling and idempotency."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.demo import build_app


@pytest.fixture()
def client() -> TestClient:
    with TestClient(build_app()) as c:
        yield c


def test_invoke_by_alias_succeeds(client: TestClient) -> None:
    r = client.post(
        "/functions/echo/aliases/current/invoke",
        json={"request_id": "http-1", "payload": {"a": 1}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "SUCCEEDED"
    assert body["result"] == {"echo": {"a": 1}}
    assert body["resolved_version"] == "v1"
    assert body["request_id"] == "http-1"
    assert body["elapsed_seconds"] is not None


def test_duplicate_http_request_converges(client: TestClient) -> None:
    payload = {"request_id": "http-2", "payload": "x"}
    r1 = client.post(
        "/functions/echo/versions/v1/invoke", json=payload
    )
    r2 = client.post(
        "/functions/echo/versions/v1/invoke", json=payload
    )
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["state"] == r2.json()["state"] == "SUCCEEDED"


def test_request_conflict_has_distinct_status(client: TestClient) -> None:
    client.post(
        "/functions/echo/versions/v1/invoke",
        json={"request_id": "http-3", "payload": 1},
    )
    r = client.post(
        "/functions/echo/versions/v1/invoke",
        json={"request_id": "http-3", "payload": 2},
    )
    assert r.status_code == 409
    assert r.json()["error_code"] == "REQUEST_CONFLICT"


def test_unpublished_version_distinct_error(client: TestClient) -> None:
    r = client.post(
        "/functions/echo/versions/v99/invoke",
        json={"request_id": "http-4", "payload": None},
    )
    assert r.status_code == 404
    assert r.json()["error_code"] == "VERSION_NOT_FOUND"


def test_handler_error_maps_to_502(client: TestClient) -> None:
    r = client.post(
        "/functions/flaky/versions/v1/invoke",
        json={"request_id": "http-5", "payload": None},
    )
    assert r.status_code == 502
    assert r.json()["state"] == "FAILED"
    assert r.json()["error_code"] == "HANDLER_ERROR"


def test_timeout_maps_to_504(client: TestClient) -> None:
    r = client.post(
        "/functions/slow/versions/v1/invoke",
        json={"request_id": "http-6", "payload": {"seconds": 5}},
    )
    assert r.status_code == 504
    assert r.json()["state"] == "TIMED_OUT"
    assert r.json()["error_code"] == "TIMED_OUT"


def test_concurrency_cap_maps_to_429(client: TestClient) -> None:
    barrier = threading.Barrier(3)
    statuses: list[int] = []
    lock = threading.Lock()

    def call(rid: str) -> None:
        barrier.wait()
        r = client.post(
            "/functions/slow/versions/v1/invoke",
            json={"request_id": rid, "payload": {"seconds": 0.5}},
        )
        with lock:
            statuses.append(r.status_code)

    threads = [
        threading.Thread(target=call, args=(f"http-cap-{i}",))
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(statuses) == [200, 200, 429]


def test_async_lifecycle_and_polling(client: TestClient) -> None:
    r = client.post(
        "/functions/echo/versions/v2/invoke-async",
        json={"request_id": "http-async-1", "payload": "z"},
    )
    assert r.status_code == 202
    assert r.json()["state"] == "ACCEPTED"

    final = None
    for _ in range(100):
        q = client.get("/requests/http-async-1")
        assert q.status_code == 200
        final = q.json()
        if final["state"] == "SUCCEEDED":
            break
        time.sleep(0.02)
    assert final is not None and final["state"] == "SUCCEEDED"
    assert final["result"] == {"echo": "z", "version": "v2"}


def test_query_unknown_request_is_404(client: TestClient) -> None:
    r = client.get("/requests/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error_code"] == "REQUEST_NOT_FOUND"


def test_alias_switch_then_invoke_hits_new_version(client: TestClient) -> None:
    r = client.put(
        "/admin/functions/echo/aliases/current",
        json={"alias": "current", "version": "v2"},
    )
    assert r.status_code == 200
    r2 = client.post(
        "/functions/echo/aliases/current/invoke",
        json={"request_id": "http-switch-1", "payload": None},
    )
    assert r2.status_code == 200
    assert r2.json()["resolved_version"] == "v2"
    # restore the demo default
    client.put(
        "/admin/functions/echo/aliases/current",
        json={"alias": "current", "version": "v1"},
    )


def test_disable_blocks_new_invocations(client: TestClient) -> None:
    client.post(
        "/admin/functions/echo/status", json={"enabled": False}
    )
    r = client.post(
        "/functions/echo/versions/v1/invoke",
        json={"request_id": "http-off-1", "payload": None},
    )
    assert r.status_code == 409
    assert r.json()["error_code"] == "FUNCTION_DISABLED"
    client.post(
        "/admin/functions/echo/status", json={"enabled": True}
    )
    r2 = client.post(
        "/functions/echo/versions/v1/invoke",
        json={"request_id": "http-off-2", "payload": None},
    )
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Weighted aliases (gray rollout) over HTTP
# ---------------------------------------------------------------------------


def test_weighted_alias_split_over_http(client: TestClient) -> None:
    r = client.put(
        "/admin/functions/echo/aliases/canary",
        json={"alias": "canary", "weights": {"v1": 50, "v2": 50}},
    )
    assert r.status_code == 200
    assert r.json()["weights"] == {"v1": 50, "v2": 50}

    g = client.get("/admin/functions/echo/aliases/canary")
    assert g.status_code == 200
    assert g.json()["weights"] == {"v1": 50, "v2": 50}

    hits: dict[str, int] = {}
    for i in range(20):
        resp = client.post(
            "/functions/echo/aliases/canary/invoke",
            json={"request_id": f"http-w-{i}", "payload": None},
        )
        assert resp.status_code == 200
        hits[resp.json()["resolved_version"]] = (
            hits.get(resp.json()["resolved_version"], 0) + 1
        )
    assert hits == {"v1": 10, "v2": 10}


def test_single_version_alias_reads_as_100_percent(client: TestClient) -> None:
    g = client.get("/admin/functions/echo/aliases/current")
    assert g.status_code == 200
    assert g.json()["weights"] == {"v1": 100}


def test_invalid_weights_rejected_with_distinct_codes(
    client: TestClient,
) -> None:
    url = "/admin/functions/echo/aliases/bad"

    r = client.put(url, json={"alias": "bad", "weights": {"v1": 60, "v2": 60}})
    assert r.status_code == 400
    assert r.json()["error_code"] == "WEIGHT_SUM_INVALID"

    r = client.put(url, json={"alias": "bad", "weights": {}})
    assert r.status_code == 400
    assert r.json()["error_code"] == "ALIAS_WEIGHTS_EMPTY"

    r = client.put(
        url, json={"alias": "bad", "weights": {"v1": -10, "v2": 110}}
    )
    assert r.status_code == 400
    assert r.json()["error_code"] == "INVALID_WEIGHT"

    r = client.put(
        url, json={"alias": "bad", "weights": {"v1": 50, "v9": 50}}
    )
    assert r.status_code == 404
    assert r.json()["error_code"] == "VERSION_NOT_FOUND"

    r = client.put(
        url,
        json={"alias": "bad", "version": "v1", "weights": {"v1": 100}},
    )
    assert r.status_code == 400
    assert r.json()["error_code"] == "INVALID_REQUEST"


def test_stats_endpoint_reports_per_version_books(
    client: TestClient,
) -> None:
    client.post(
        "/functions/echo/versions/v1/invoke",
        json={"request_id": "http-st-1", "payload": None},
    )
    client.post(
        "/functions/echo/versions/v2/invoke",
        json={"request_id": "http-st-2", "payload": None},
    )
    client.post(
        "/functions/flaky/versions/v1/invoke",
        json={"request_id": "http-st-3", "payload": None},
    )
    r = client.get("/admin/functions/echo/stats")
    assert r.status_code == 200
    versions = {v["version"]: v for v in r.json()["versions"]}
    assert versions["v1"]["succeeded"] == 1
    assert versions["v2"]["succeeded"] == 1
    assert versions["v1"]["inflight"] == 0
    assert versions["v1"]["max_concurrency"] == 8

    r = client.get("/admin/functions/flaky/stats")
    versions = {v["version"]: v for v in r.json()["versions"]}
    assert versions["v1"]["failed"] == 1
