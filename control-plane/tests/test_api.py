from __future__ import annotations

import re
from typing import Any

from fastapi.testclient import TestClient

from app.main import Platform, create_app
from app.models import FakeClock, Status

from .conftest import ADMIN, ALICE, BOB, ParkedExecutor, auth


def create(client: TestClient, branch: str, token: str = ALICE, ttl: str = "1h"):
    return client.post("/v1/envs", json={"branch": branch, "ttl": ttl}, headers=auth(token))


def assert_problem(resp, status: int) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert {"type", "title", "status", "detail", "instance"} <= body.keys()
    assert body["status"] == status
    return body


def test_health_ready_metrics(client: TestClient, platform: Platform) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").status_code == 200
    platform.runtime.up = False  # type: ignore[attr-defined]
    ready = client.get("/readyz")
    assert ready.status_code == 503 and ready.json()["checks"]["docker"] is False
    assert "ephemeral_envs_created_total" in client.get("/metrics").text


def test_create_returns_202_with_dsn_once(client: TestClient, platform: Platform) -> None:
    resp = create(client, "feature/Checkout")
    assert resp.status_code == 202
    body = resp.json()
    assert body["slug"] == "feature-checkout"
    assert body["status"] == "PROVISIONING"
    assert body["url"] == "http://feature-checkout.localhost:8080"
    assert re.match(r"^postgresql://app:[A-Za-z0-9_-]{20,}@db:5432/app$", body["dsn"])
    password = body["dsn"].split(":")[2].split("@")[0]
    status = client.get("/v1/envs/feature-checkout", headers=auth(ALICE))
    assert status.json()["status"] == "RUNNING"
    assert password not in status.text and "dsn" not in status.json()
    listing = client.get("/v1/envs", headers=auth(ALICE))
    assert password not in listing.text
    audit = client.get("/v1/audit?limit=100", headers=auth(ADMIN))
    assert password not in audit.text


def test_authentication_required(client: TestClient) -> None:
    body = assert_problem(client.get("/v1/envs"), 401)
    assert body["code"] == "unauthenticated"
    assert_problem(client.get("/v1/envs", headers={"Authorization": "Basic x"}), 401)
    assert_problem(client.get("/v1/envs", headers=auth("wrong-token")), 401)
    resp = client.get("/v1/envs", headers=auth("wrong"))
    assert resp.headers["www-authenticate"] == "Bearer"


def test_cross_owner_access_denied(client: TestClient) -> None:
    assert create(client, "alice-env").status_code == 202
    for method, path, kw in [
        ("GET", "/v1/envs/alice-env", {}),
        ("POST", "/v1/envs/alice-env/extend", {"json": {"by": "1h"}}),
        ("DELETE", "/v1/envs/alice-env", {}),
    ]:
        resp = client.request(method, path, headers=auth(BOB), **kw)
        assert assert_problem(resp, 403)["code"] == "not-owner"
    assert client.get("/v1/envs", headers=auth(BOB)).json()["count"] == 0
    assert client.get("/v1/envs", headers=auth(ADMIN)).json()["count"] == 1
    assert client.get("/v1/envs/alice-env", headers=auth(ADMIN)).status_code == 200


def test_admin_only_endpoints(client: TestClient) -> None:
    assert_problem(client.get("/v1/audit", headers=auth(ALICE)), 403)
    assert_problem(client.get("/v1/finops/summary", headers=auth(ALICE)), 403)
    create(client, "x1")
    summary = client.get("/v1/finops/summary", headers=auth(ADMIN)).json()
    assert summary["active_envs"] == 1
    assert summary["reserved_memory_mb"] == 192
    assert "estimate" in summary["unit"]
    audit = client.get("/v1/audit?limit=5", headers=auth(ADMIN)).json()
    assert audit["items"][0]["ts"].endswith("Z")
    assert_problem(client.get("/v1/audit?limit=0", headers=auth(ADMIN)), 400)


def test_validation_errors_are_problem_json(client: TestClient) -> None:
    body = assert_problem(create(client, "admin"), 400)
    assert "reserved" in body["detail"]
    assert_problem(create(client, "ok", ttl="forever"), 400)
    assert_problem(create(client, "ok", ttl="5s"), 400)
    assert_problem(create(client, "ok", ttl="999h"), 400)
    body = assert_problem(client.post("/v1/envs", json={}, headers=auth(ALICE)), 400)
    assert body["code"] == "validation-error" and body["errors"]
    assert_problem(client.get("/v1/envs/nope", headers=auth(ALICE)), 404)
    assert_problem(client.get("/does-not-exist"), 404)


def test_slug_conflict(client: TestClient) -> None:
    assert create(client, "feat/x").status_code == 202
    body = assert_problem(create(client, "feat_x", token=BOB), 409)
    assert body["code"] == "slug-conflict"


def test_quota_429_with_retry_after(client: TestClient, platform: Platform) -> None:
    for i in range(3):
        assert create(client, f"a-{i}").status_code == 202
    resp = create(client, "a-3")
    body = assert_problem(resp, 429)
    assert body["reason"] == "per_owner"
    assert int(resp.headers["retry-after"]) > 0
    assert create(client, "b-0", token=BOB).status_code == 202
    metrics = client.get("/metrics").text
    assert 'ephemeral_quota_rejections_total{reason="per_owner"} 1.0' in metrics


def test_extend(client: TestClient, clock: FakeClock) -> None:
    created = create(client, "ext", ttl="1h").json()
    resp = client.post("/v1/envs/ext/extend", json={"by": "2h"}, headers=auth(ALICE))
    assert resp.status_code == 200
    body = resp.json()
    assert body["previous_expires_at"] == created["expires_at"]
    assert body["expires_at"] > created["expires_at"]
    assert body["extensions"] == 1 and body["capped_at_hard_deadline"] is False
    capped = client.post("/v1/envs/ext/extend", json={"by": "30d"}, headers=auth(ALICE)).json()
    assert capped["capped_at_hard_deadline"] is True
    assert capped["expires_at"] == capped["hard_deadline"]
    assert_problem(client.post("/v1/envs/ext/extend", json={"by": "1h"}, headers=auth(ALICE)), 409)
    assert_problem(client.post("/v1/envs/ext/extend", json={"by": "zz"}, headers=auth(ALICE)), 400)


def test_extend_requires_running(client: TestClient, platform: Platform) -> None:
    create(client, "gone")
    client.delete("/v1/envs/gone", headers=auth(ALICE))
    resp = client.post("/v1/envs/gone/extend", json={"by": "1h"}, headers=auth(ALICE))
    assert assert_problem(resp, 409)["code"] == "bad-state"


def test_delete_is_idempotent(client: TestClient, platform: Platform) -> None:
    create(client, "del")
    first = client.delete("/v1/envs/del", headers=auth(ALICE))
    assert first.status_code == 202 and first.json()["status"] == "EXPIRING"
    second = client.delete("/v1/envs/del", headers=auth(ALICE))
    assert second.status_code == 202 and second.json()["status"] == "EXPIRING"
    platform.reconciler.run_once()
    third = client.delete("/v1/envs/del", headers=auth(ALICE))
    assert third.status_code == 202 and third.json()["status"] == "DROPPED"
    assert not platform.runtime.list_managed()


def test_delete_drop_failed_rearms_retry(client: TestClient, platform: Platform) -> None:
    create(client, "stuck")
    platform.runtime.fail.add("remove_volume")  # type: ignore[attr-defined]
    client.delete("/v1/envs/stuck", headers=auth(ALICE))
    platform.reconciler.run_once()
    env = platform.store.get_by_slug("stuck")
    assert env.status is Status.DROP_FAILED
    resp = client.delete("/v1/envs/stuck", headers=auth(ALICE))
    assert resp.status_code == 202 and resp.json()["status"] == "DROP_FAILED"
    assert platform.store.get(env.id).teardown_attempts == 0


def test_delete_failed_env_is_noop(client: TestClient, platform: Platform) -> None:
    platform.runtime.fail.add("create_network")  # type: ignore[attr-defined]
    assert create(client, "broken").status_code == 202
    resp = client.delete("/v1/envs/broken", headers=auth(ALICE))
    assert resp.status_code == 202 and resp.json()["status"] == "FAILED"
    assert resp.json()["failure_reason"].startswith("network:")


def test_delete_pending_returns_409(make_platform, clock: FakeClock) -> None:
    platform = make_platform()
    platform.provisioner.executor = ParkedExecutor()
    with TestClient(create_app(platform, background=False)) as c:
        assert create(c, "parked").json()["status"] == "PROVISIONING"
        assert_problem(c.delete("/v1/envs/parked", headers=auth(ALICE)), 409)


def test_list_all_includes_terminal(client: TestClient, platform: Platform) -> None:
    create(client, "t1")
    client.delete("/v1/envs/t1", headers=auth(ALICE))
    platform.reconciler.run_once()
    assert client.get("/v1/envs", headers=auth(ALICE)).json()["count"] == 0
    items = client.get("/v1/envs?all=true", headers=auth(ALICE)).json()["items"]
    assert [i["status"] for i in items] == ["DROPPED"]


def test_metrics_have_no_per_slug_labels(client: TestClient, platform: Platform) -> None:
    create(client, "very-unique-slug-name")
    platform.reconciler.run_once()
    text = client.get("/metrics").text
    assert "very-unique-slug-name" not in text
    assert "alice" not in text
    label_keys = set(re.findall(r"\{([a-z_]+)=", text))
    assert label_keys <= {"status", "reason", "le"}
    assert 'ephemeral_envs_active{status="RUNNING"} 1.0' in text
