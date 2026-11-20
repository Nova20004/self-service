from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace

import pytest

from app.models import (
    TRANSITIONS,
    Environment,
    FakeClock,
    IllegalTransition,
    Status,
    check_transition,
)
from app.quota import Limits, QuotaExceeded, Usage, check
from app.store import BadState, NotFound, SlugConflict, Store

LIMITS = Limits(max_envs_per_owner=3, max_envs_total=5, max_total_memory_mb=1024)


def make_env(
    i: int = 1,
    *,
    owner: str = "alice",
    slug: str | None = None,
    now: float = 1000.0,
    ttl: float = 600,
    memory: int = 192,
) -> Environment:
    return Environment(
        id=f"id{i:06d}",
        slug=slug or f"env-{i}",
        owner=owner,
        branch=f"b/{i}",
        status=Status.PENDING,
        created_at=now,
        expires_at=now + ttl,
        hard_deadline=now + 3600,
        extensions=0,
        memory_mb=memory,
        estimated_hourly_cost=memory / 1024 * 0.01,
        failure_reason=None,
        updated_at=now,
    )


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store(":memory:")
    yield s
    s.close()


def to_running(store: Store, env_id: str, now: float = 1000.0) -> Environment:
    store.transition(env_id, Status.PROVISIONING, now=now, actor="t")
    return store.transition(env_id, Status.RUNNING, now=now, actor="t")


ALL_EDGES = [(a, b) for a in Status for b in Status]


@pytest.mark.parametrize(("current", "target"), ALL_EDGES)
def test_state_machine(current: Status, target: Status) -> None:
    if target in TRANSITIONS[current]:
        check_transition(current, target)
    else:
        with pytest.raises(IllegalTransition):
            check_transition(current, target)


def test_happy_path_and_illegal_transition_in_store(store: Store) -> None:
    store.reserve(make_env(), limits=LIMITS, actor="alice")
    with pytest.raises(IllegalTransition):
        store.transition("id000001", Status.RUNNING, now=1, actor="t")
    env = to_running(store, "id000001")
    assert env.status is Status.RUNNING
    with pytest.raises(IllegalTransition):
        store.transition("id000001", Status.DROPPED, now=1, actor="t")
    store.transition("id000001", Status.EXPIRING, now=2, actor="t")
    dropped = store.transition("id000001", Status.DROPPED, now=3, actor="t")
    assert dropped.ended_at == 3
    with pytest.raises(IllegalTransition):
        store.transition("id000001", Status.RUNNING, now=4, actor="t")


def test_transition_expected_guard_and_not_found(store: Store) -> None:
    store.reserve(make_env(), limits=LIMITS, actor="alice")
    with pytest.raises(BadState):
        store.transition(
            "id000001", Status.PROVISIONING, now=1, actor="t", expected=[Status.RUNNING]
        )
    with pytest.raises(NotFound):
        store.transition("missing", Status.RUNNING, now=1, actor="t")
    with pytest.raises(NotFound):
        store.get("missing")
    with pytest.raises(NotFound):
        store.get_by_slug("missing")


def test_live_slug_unique_but_reusable_after_drop(store: Store) -> None:
    store.reserve(make_env(1, slug="same"), limits=LIMITS, actor="a")
    with pytest.raises(SlugConflict):
        store.reserve(make_env(2, slug="same"), limits=LIMITS, actor="a")
    to_running(store, "id000001")
    store.transition("id000001", Status.EXPIRING, now=2, actor="t")
    store.transition("id000001", Status.DROPPED, now=3, actor="t")
    store.reserve(make_env(3, slug="same", now=5), limits=LIMITS, actor="a")
    assert store.get_by_slug("same").id == "id000003"


def test_partial_unique_index_enforced_by_sqlite(store: Store) -> None:
    store.reserve(make_env(1, slug="dup"), limits=LIMITS, actor="a")
    conn = store._conn
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO envs (id, slug, owner, branch, status, created_at, expires_at, "
            "hard_deadline, memory_mb, estimated_hourly_cost, updated_at) "
            "VALUES ('x', 'dup', 'a', 'b', 'RUNNING', 0, 0, 0, 1, 0, 0)"
        )


def test_audit_is_append_only(store: Store) -> None:
    store.audit(1.0, "alice", "test", "slug", {"k": "v"})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE audit SET actor = 'mallory'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM audit")
    entries = store.list_audit(10)
    assert entries[0]["actor"] == "alice" and entries[0]["details"] == {"k": "v"}


def test_audit_never_contains_passwords(store: Store) -> None:
    store.reserve(make_env(), limits=LIMITS, actor="alice")
    dump = "\n".join(store._conn.iterdump())
    assert "password" not in dump.lower()


def test_quota_per_owner_total_and_memory(store: Store) -> None:
    for i in range(3):
        store.reserve(make_env(i), limits=LIMITS, actor="alice")
    with pytest.raises(QuotaExceeded) as exc:
        store.reserve(make_env(9), limits=LIMITS, actor="alice")
    assert exc.value.reason == "per_owner"
    assert exc.value.retry_after == 601
    store.reserve(make_env(10, owner="bob"), limits=LIMITS, actor="bob")
    store.reserve(make_env(11, owner="bob"), limits=LIMITS, actor="bob")
    with pytest.raises(QuotaExceeded) as exc:
        store.reserve(make_env(12, owner="carol"), limits=LIMITS, actor="carol")
    assert exc.value.reason == "total"


def test_quota_memory(store: Store) -> None:
    limits = Limits(max_envs_per_owner=10, max_envs_total=10, max_total_memory_mb=400)
    store.reserve(make_env(1), limits=limits, actor="a")
    store.reserve(make_env(2, owner="b"), limits=limits, actor="b")
    with pytest.raises(QuotaExceeded) as exc:
        store.reserve(make_env(3, owner="c"), limits=limits, actor="c")
    assert exc.value.reason == "memory"


def test_terminal_envs_do_not_count_against_quota(store: Store) -> None:
    for i in range(3):
        store.reserve(make_env(i), limits=LIMITS, actor="alice")
    store.transition("id000000", Status.FAILED, now=2, actor="t", reason="x")
    store.reserve(make_env(5), limits=LIMITS, actor="alice")


def test_quota_check_without_expiry_has_no_retry_after() -> None:
    usage = Usage(
        owner_live=1, total_live=1, reserved_mb=0, owner_next_expiry=None, global_next_expiry=None
    )
    with pytest.raises(QuotaExceeded) as exc:
        check(Limits(1, 5, 1000), usage, request_mb=1, owner="a", now=0)
    assert exc.value.retry_after is None


def test_extend_caps_at_hard_deadline(store: Store) -> None:
    store.reserve(make_env(), limits=LIMITS, actor="alice")
    with pytest.raises(BadState, match="only RUNNING"):
        store.extend("id000001", by_seconds=60, now=1, actor="alice")
    to_running(store, "id000001")
    env, capped = store.extend("id000001", by_seconds=600, now=1, actor="alice")
    assert env.expires_at == 2200 and not capped and env.extensions == 1
    env, capped = store.extend("id000001", by_seconds=99999, now=2, actor="alice")
    assert env.expires_at == env.hard_deadline == 4600 and capped
    with pytest.raises(BadState, match="maximum total lifetime"):
        store.extend("id000001", by_seconds=60, now=3, actor="alice")
    with pytest.raises(NotFound):
        store.extend("nope", by_seconds=60, now=3, actor="alice")


def test_list_and_counts_and_finops(store: Store) -> None:
    store.reserve(make_env(1), limits=LIMITS, actor="a")
    store.reserve(make_env(2, owner="bob"), limits=LIMITS, actor="b")
    store.transition("id000002", Status.FAILED, now=1000 + 1800, actor="t")
    assert [e.id for e in store.list_envs(owner="alice")] == ["id000001"]
    assert len(store.list_envs()) == 1
    assert len(store.list_envs(include_terminal=True)) == 2
    counts = store.counts_by_status()
    assert counts[Status.PENDING] == 1 and counts[Status.FAILED] == 1
    summary = store.finops(1000 + 3600)
    assert summary["active_envs"] == 1
    assert summary["reserved_memory_mb"] == 192
    assert summary["estimated_cost_per_hour"] == pytest.approx(0.001875)
    # one env for 1 h + one env for 0.5 h at 0.001875/h
    assert summary["estimated_cost_since_start"] == pytest.approx(0.001875 * 1.5, abs=1e-6)


def test_reset_teardown(store: Store) -> None:
    store.reserve(make_env(), limits=LIMITS, actor="a")
    to_running(store, "id000001")
    store.transition("id000001", Status.EXPIRING, now=2, actor="t")
    store.transition("id000001", Status.DROP_FAILED, now=3, actor="t", teardown_attempts=5)
    env = store.reset_teardown("id000001", now=10, actor="admin")
    assert env.teardown_attempts == 0 and env.next_attempt_at == 10
    assert store.get("id000001").teardown_attempts == 0


def test_store_persists_on_disk(tmp_path) -> None:
    path = str(tmp_path / "state.db")
    s1 = Store(path)
    s1.reserve(make_env(), limits=LIMITS, actor="a")
    s1.close()
    s2 = Store(path)
    assert s2.get("id000001").slug == "env-1"
    assert s2._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    s2.close()


def test_fake_clock() -> None:
    c = FakeClock(10)
    c.sleep(5)
    c.advance(1)
    assert c.now() == 16


def test_environment_public_shape() -> None:
    env = replace(make_env(), status=Status.RUNNING)
    data = env.to_public("http://x")
    assert data["created_at"] == "1970-01-01T00:16:40Z"
    assert "password" not in data and "dsn" not in data
