"""HTTP 调用协议端到端：注册冲突、调用、异步查询、去重。"""

import time

from fastapi.testclient import TestClient

from main import create_app


def _client():
    return TestClient(create_app())


def test_health_and_builtins():
    c = _client()
    assert c.get("/health").json() == {"status": "ok"}
    assert "echo" in c.get("/builtins").json()["builtins"]


def test_register_conflict_and_immutable():
    c = _client()
    body = {"builtin": "echo", "timeout_seconds": 3, "concurrency_limit": 2}
    r1 = c.post("/functions/f/versions/v1", json=body)
    assert r1.status_code == 200 and r1.json()["immutable"] is True

    r2 = c.post("/functions/f/versions/v1", json=body)
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "version_already_exists"


def test_invoke_unpublished_version_distinguishable():
    c = _client()
    c.post("/functions/f/versions/v1", json={"builtin": "echo"})
    r = c.post("/invoke/f/v9", json={"request_id": "h1", "payload": {"x": 1}})
    data = r.json()
    assert data["state"] == "rejected_no_version"
    assert "version_not_found" in data["payload"]


def test_sync_invoke_and_idempotent_request_id():
    c = _client()
    c.post("/functions/add/versions/v1", json={"builtin": "add"})
    payload = {"request_id": "add-1", "payload": {"a": 1, "b": 2}}
    r1 = c.post("/invoke/add/v1", json=payload).json()
    assert r1["state"] == "succeeded" and r1["payload"] == 3.0

    # 不同载荷 + 同一 request_id -> 同一结论
    r2 = c.post("/invoke/add/v1",
                json={"request_id": "add-1", "payload": {"a": 100, "b": 200}}).json()
    assert r2 == r1


def test_concurrency_rejection_over_http():
    c = _client()
    c.post("/functions/s/versions/v1",
           json={"builtin": "sleep_echo", "timeout_seconds": 5, "concurrency_limit": 1})

    first = c.post("/invoke-async/s/v1",
                   json={"request_id": "c1", "payload": {"seconds": 1}}).json()
    assert first["state"] == "running"
    time.sleep(0.15)

    rejected = c.post("/invoke/s/v1",
                      json={"request_id": "c2", "payload": {"seconds": 1}}).json()
    assert rejected["state"] == "concurrency_rejected"


def test_async_query_and_timeout():
    c = _client()
    c.post("/functions/slow/versions/v1",
           json={"builtin": "sleep_echo", "timeout_seconds": 0.05, "concurrency_limit": 2})

    r = c.post("/invoke-async/slow/v1",
               json={"request_id": "to1", "payload": {"seconds": 0.4}}).json()
    assert r["state"] in ("running", "timed_out")

    final = c.get("/requests/to1").json()
    deadline = time.time() + 3
    while final["state"] == "running":
        time.sleep(0.02)
        final = c.get("/requests/to1").json()
    assert final["state"] == "timed_out"
    assert any("TIMED_OUT" in line for line in final["transitions"])


def test_alias_lifecycle_over_http():
    c = _client()
    c.post("/functions/f/versions/v1", json={"builtin": "echo"})
    c.post("/functions/f/versions/v2", json={"builtin": "add"})

    # 别名指向未发布版本 -> 404 version_not_found
    r = c.put("/functions/f/aliases/prod", json={"version": "v9"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "version_not_found"

    assert c.put("/functions/f/aliases/prod", json={"version": "v1"}).status_code == 200
    r1 = c.post("/invoke/f/prod", json={"request_id": "p1", "payload": {"k": 1}}).json()
    assert r1["state"] == "succeeded" and r1["target"] == "v1"

    c.put("/functions/f/aliases/prod", json={"version": "v2"})
    r2 = c.post("/invoke/f/prod",
                json={"request_id": "p2", "payload": {"a": 2, "b": 3}}).json()
    assert r2["target"] == "v2" and r2["payload"] == 5.0

    # 停用 -> 新请求被区分拒绝；重新启用恢复
    c.post("/functions/f/disable")
    off = c.post("/invoke/f/prod", json={"request_id": "p3", "payload": None}).json()
    assert off["state"] == "rejected_disabled"
    c.post("/functions/f/enable")
    ok = c.post("/invoke/f/v1", json={"request_id": "p4", "payload": "back"}).json()
    assert ok["state"] == "succeeded"
