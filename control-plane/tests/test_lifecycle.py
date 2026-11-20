from __future__ import annotations

import pytest

from app.main import Platform
from app.models import FakeClock, Status
from app.provisioner import (
    POSTGRES_UID,
    WORKLOAD_UID,
    app_name,
    db_name,
    network_name,
    volume_name,
)
from app.runtime import (
    MANAGED_BY_KEY,
    MANAGED_BY_VALUE,
    ContainerSpec,
    FakeRuntime,
)
from app.store import Store

from .conftest import ParkedExecutor


def request(p: Platform, branch: str = "feat/a", ttl: str = "1h", owner: str = "alice"):
    return p.provisioner.request(owner=owner, branch=branch, ttl=ttl, actor=owner)


def test_happy_path_creates_isolated_hardened_resources(
    platform: Platform, runtime: FakeRuntime
) -> None:
    env = request(platform).env
    assert platform.store.get(env.id).status is Status.RUNNING
    assert runtime.calls == [
        f"create_network:{network_name(env.id)}",
        f"create_volume:{volume_name(env.id)}",
        "run_container:db",
        "run_container:app",
        f"connect:{network_name(env.id)}",
    ]
    assert "eidp-traefik" in runtime.networks[network_name(env.id)]["members"]
    app = runtime.containers[app_name(env.id)].spec
    db = runtime.containers[db_name(env.id)].spec
    for spec in (app, db):
        assert spec.labels[MANAGED_BY_KEY] == MANAGED_BY_VALUE
        assert spec.labels["ephemeral.env-id"] == env.id
        assert spec.labels["ephemeral.owner"] == "alice"
        assert spec.labels["ephemeral.slug"] == "feat-a"
        assert spec.labels["ephemeral.expires-at"].endswith("Z")
        assert spec.read_only and spec.cap_add == []
        assert spec.network == network_name(env.id)
    assert app.user == WORKLOAD_UID and db.user == POSTGRES_UID
    assert app.memory_mb == 64 and db.memory_mb == 128
    assert app.labels["traefik.enable"] == "true"
    assert app.labels["traefik.docker.network"] == network_name(env.id)
    assert "traefik.enable" not in db.labels
    assert db.aliases == ["db"]
    assert app.env["DATABASE_URL"].endswith("@db:5432/app")


STEPS = [
    ("create_network", "network"),
    ("create_volume", "volume"),
    ("run_container:db", "db"),
    ("run_container:app", "app"),
    ("connect", "route"),
]


@pytest.mark.parametrize(("fail_key", "step"), STEPS)
def test_rollback_on_every_failing_step(
    platform: Platform, runtime: FakeRuntime, fail_key: str, step: str
) -> None:
    runtime.fail.add(fail_key)
    env = request(platform).env
    stored = platform.store.get(env.id)
    assert stored.status is Status.FAILED
    assert stored.failure_reason is not None and stored.failure_reason.startswith(f"{step}:")
    assert runtime.list_managed() == []
    assert not runtime.containers and not runtime.networks and not runtime.volumes
    actions = [e["action"] for e in platform.store.list_audit(20)]
    assert "env.failed" in actions
    assert platform.metrics.provision_failures._value.get() == 1


@pytest.mark.parametrize(
    ("role", "health"),
    [("db", "unhealthy"), ("app", "unhealthy"), ("db", "starting"), ("app", "starting")],
)
def test_rollback_on_health_failure_or_timeout(
    platform: Platform, runtime: FakeRuntime, role: str, health: str
) -> None:
    runtime.health[role] = health
    env = request(platform).env
    stored = platform.store.get(env.id)
    assert stored.status is Status.FAILED
    assert role in (stored.failure_reason or "")
    assert not runtime.containers and not runtime.networks


def test_rollback_on_exited_container(
    platform: Platform, runtime: FakeRuntime, monkeypatch
) -> None:
    original = runtime.run_container

    def run_and_crash(spec: ContainerSpec) -> None:
        original(spec)
        if spec.labels["ephemeral.role"] == "db":
            runtime.containers[spec.name].status = "exited"

    monkeypatch.setattr(runtime, "run_container", run_and_crash)
    env = request(platform).env
    assert "db container exited" in (platform.store.get(env.id).failure_reason or "")


def test_missing_image_fails_cleanly(platform: Platform, runtime: FakeRuntime) -> None:
    runtime.images = {"something-else"}
    env = request(platform).env
    stored = platform.store.get(env.id)
    assert stored.status is Status.FAILED and "images:" in (stored.failure_reason or "")


def test_rollback_failure_is_reported(platform: Platform, runtime: FakeRuntime) -> None:
    runtime.fail.update({"run_container:app", "remove_volume"})
    env = request(platform).env
    stored = platform.store.get(env.id)
    assert stored.status is Status.FAILED
    assert "rollback incomplete" in (stored.failure_reason or "")
    # the orphan reaper finishes the job once the runtime recovers
    runtime.fail.clear()
    platform.reconciler.clock.advance(31)  # type: ignore[attr-defined]
    report = platform.reconciler.run_once()
    assert f"volume:{volume_name(env.id)}" in report.orphans
    assert runtime.list_managed() == []


def test_reaper_expires_and_tears_down_in_order(
    platform: Platform, runtime: FakeRuntime, clock: FakeClock
) -> None:
    env = request(platform, ttl="20s").env
    platform.reconciler.run_once()
    assert platform.store.get(env.id).status is Status.RUNNING
    clock.advance(21)
    runtime.calls.clear()
    report = platform.reconciler.run_once()
    assert report.expired == ["feat-a"] and report.dropped == ["feat-a"]
    assert runtime.calls == [
        "remove_container:app",
        "remove_container:db",
        f"disconnect:{network_name(env.id)}",
        f"remove_network:{network_name(env.id)}",
        f"remove_volume:{volume_name(env.id)}",
    ]
    stored = platform.store.get(env.id)
    assert stored.status is Status.DROPPED and stored.ended_at == clock.now()
    assert platform.metrics.envs_reaped._value.get() == 1
    actors = {e["actor"] for e in platform.store.list_audit(50) if e["action"] == "env.expiring"}
    assert actors == {"reaper"}


def test_teardown_is_idempotent(platform: Platform, runtime: FakeRuntime) -> None:
    env = request(platform).env
    platform.provisioner.teardown(env)
    platform.provisioner.teardown(env)
    assert runtime.list_managed() == []


def test_partial_teardown_failure_retries_with_backoff(
    platform: Platform, runtime: FakeRuntime, clock: FakeClock
) -> None:
    env = request(platform, ttl="20s").env
    runtime.fail.add("remove_network")
    runtime.fail_counts["remove_network"] = 2
    clock.advance(21)
    platform.reconciler.run_once()
    first = platform.store.get(env.id)
    assert first.status is Status.DROP_FAILED and first.teardown_attempts == 1
    assert first.next_attempt_at == clock.now() + 2
    # containers already removed on the first attempt; retry is not due yet
    assert app_name(env.id) not in runtime.containers
    platform.reconciler.run_once()
    assert platform.store.get(env.id).teardown_attempts == 1
    clock.advance(2)
    platform.reconciler.run_once()
    second = platform.store.get(env.id)
    assert second.teardown_attempts == 2 and second.next_attempt_at == clock.now() + 4
    clock.advance(4)
    platform.reconciler.run_once()
    assert platform.store.get(env.id).status is Status.DROPPED
    assert runtime.list_managed() == []


def test_teardown_gives_up_after_bounded_attempts(
    make_platform, runtime: FakeRuntime, clock: FakeClock
) -> None:
    p = make_platform(teardown_max_attempts=2)
    env = request(p, ttl="20s").env
    runtime.fail.add("remove_volume")
    clock.advance(21)
    p.reconciler.run_once()
    clock.advance(10)
    p.reconciler.run_once()
    stored = p.store.get(env.id)
    assert stored.status is Status.DROP_FAILED and stored.teardown_attempts == 2
    assert stored.next_attempt_at is None
    assert p.metrics.teardown_giveups._value.get() == 1
    clock.advance(100)
    p.reconciler.run_once()
    assert p.store.get(env.id).teardown_attempts == 2


def test_orphans_reaped_only_after_grace(
    platform: Platform, runtime: FakeRuntime, clock: FakeClock
) -> None:
    live = request(platform).env
    labels = {
        MANAGED_BY_KEY: MANAGED_BY_VALUE,
        "ephemeral.env-id": "orphan01",
        "ephemeral.role": "app",
    }
    runtime.create_network("eidp-env-orphan01", labels)
    runtime.create_volume("eidp-orphan01-pgdata", labels)
    runtime.run_container(
        ContainerSpec(
            name="eidp-orphan01-app", image="x", network="eidp-env-orphan01", labels=labels
        )
    )
    runtime.connect("eidp-env-orphan01", "eidp-traefik")
    runtime.create_network("unrelated", {"owner": "someone-else"})
    assert platform.reconciler.run_once().orphans == []
    clock.advance(31)
    report = platform.reconciler.run_once()
    assert report.orphans == [
        "container:eidp-orphan01-app",
        "network:eidp-env-orphan01",
        "volume:eidp-orphan01-pgdata",
    ]
    assert "unrelated" in runtime.networks
    assert network_name(live.id) in runtime.networks
    assert platform.store.get(live.id).status is Status.RUNNING
    assert platform.metrics.orphans_reaped._value.get() == 3


def test_unlabelled_resources_never_touched(
    platform: Platform, runtime: FakeRuntime, clock: FakeClock
) -> None:
    runtime.create_network("users-own-net", {})
    runtime.create_volume("users-own-vol", {"managed-by": "someone-else"})
    clock.advance(3600)
    platform.reconciler.run_once()
    assert "users-own-net" in runtime.networks and "users-own-vol" in runtime.volumes


def test_orphan_listing_error_is_reported(platform: Platform, runtime: FakeRuntime) -> None:
    runtime.fail.add("list_managed")
    report = platform.reconciler.run_once()
    assert report.errors and "list managed" in report.errors[0]


def test_orphan_removal_error_is_reported(
    platform: Platform, runtime: FakeRuntime, clock: FakeClock
) -> None:
    runtime.create_volume("eidp-zz-pgdata", {MANAGED_BY_KEY: MANAGED_BY_VALUE})
    runtime.fail.add("remove_volume")
    clock.advance(31)
    assert platform.reconciler.run_once().errors


def test_running_env_with_vanished_containers_is_cleaned(
    platform: Platform, runtime: FakeRuntime
) -> None:
    env = request(platform).env
    del runtime.containers[app_name(env.id)]
    report = platform.reconciler.run_once()
    assert report.drifted == ["feat-a"] and report.dropped == ["feat-a"]
    stored = platform.store.get(env.id)
    assert stored.status is Status.DROPPED
    assert "vanished" in (stored.failure_reason or "")
    assert runtime.list_managed() == []


def test_crash_recovery_mid_provisioning(
    settings, clock: FakeClock, runtime: FakeRuntime, tmp_path, make_platform
) -> None:
    db = str(tmp_path / "state.db")
    first = make_platform(store=Store(db))
    # simulate a crash: the worker never runs, resources were partially created
    first.provisioner.executor = ParkedExecutor()
    env = request(first).env
    runtime.create_network(network_name(env.id), first.provisioner.labels(env, "network"))
    first.store.close()

    second = make_platform(store=Store(db))
    report = second.reconciler.run_once()
    assert report.failed == ["feat-a"]
    stored = second.store.get(env.id)
    assert stored.status is Status.FAILED
    assert "restarted during provisioning" in (stored.failure_reason or "")
    assert runtime.list_managed() == []


def test_crash_recovery_keeps_running_envs_and_reaper_continues(
    clock: FakeClock, runtime: FakeRuntime, tmp_path, make_platform
) -> None:
    db = str(tmp_path / "state.db")
    first = make_platform(store=Store(db))
    keep = request(first, branch="keep", ttl="1h").env
    short = request(first, branch="short", ttl="20s").env
    first.store.close()

    second = make_platform(store=Store(db))
    second.reconciler.run_once()
    assert second.store.get(keep.id).status is Status.RUNNING
    assert second.store.get(short.id).status is Status.RUNNING
    clock.advance(21)
    second.reconciler.run_once()
    assert second.store.get(short.id).status is Status.DROPPED
    assert second.store.get(keep.id).status is Status.RUNNING
    assert app_name(keep.id) in runtime.containers


def test_provision_timeout(platform: Platform, clock: FakeClock) -> None:
    platform.provisioner.executor = ParkedExecutor()
    env = request(platform).env
    platform.reconciler.run_once()
    assert platform.store.get(env.id).status is Status.PROVISIONING
    clock.advance(61)
    platform.reconciler.run_once()
    stored = platform.store.get(env.id)
    assert stored.status is Status.FAILED and "exceeded" in (stored.failure_reason or "")
    # the late worker notices and leaves nothing behind
    platform.provisioner._provision(env.id, "pw")
    assert not platform.runtime.list_managed()


def test_late_worker_after_timeout_tears_down(
    platform: Platform, runtime: FakeRuntime, monkeypatch
) -> None:
    calls: dict[str, int] = {"n": 0}
    original = runtime.connect

    def connect_then_timeout(network: str, container: str) -> None:
        original(network, container)
        env = platform.store.by_status([Status.PROVISIONING])[0]
        platform.store.transition(
            env.id, Status.FAILED, now=0, actor="reconciler", reason="timeout"
        )
        calls["n"] += 1

    monkeypatch.setattr(runtime, "connect", connect_then_timeout)
    env = request(platform).env
    assert calls["n"] == 1
    assert platform.store.get(env.id).status is Status.FAILED
    assert runtime.list_managed() == []


def test_cost_units_accrue(platform: Platform, clock: FakeClock) -> None:
    request(platform, ttl="2h")
    platform.reconciler.run_once()
    clock.advance(3600)
    platform.reconciler.run_once()
    assert platform.metrics.cost_units._value.get() == pytest.approx(192 / 1024 * 0.01)


def test_background_loop_runs_immediately(platform: Platform) -> None:
    import time

    platform.reconciler.start()
    deadline = time.monotonic() + 2
    while platform.reconciler.passes == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    platform.reconciler.stop()
    assert platform.reconciler.passes >= 1
