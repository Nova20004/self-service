"""SQLite state store (WAL, single writer, explicit transactions).

Passwords are never written here. The audit table is append-only: triggers
abort any UPDATE or DELETE on it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from .models import LIVE, Environment, Status, check_transition
from .quota import Limits, Usage, check

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS envs (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    owner TEXT NOT NULL,
    branch TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    hard_deadline REAL NOT NULL,
    extensions INTEGER NOT NULL DEFAULT 0,
    memory_mb INTEGER NOT NULL,
    estimated_hourly_cost REAL NOT NULL,
    failure_reason TEXT,
    updated_at REAL NOT NULL,
    ended_at REAL,
    teardown_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_envs_live_slug
    ON envs(slug) WHERE status NOT IN ('DROPPED', 'FAILED');
CREATE INDEX IF NOT EXISTS ix_envs_owner_status ON envs(owner, status);
CREATE INDEX IF NOT EXISTS ix_envs_status ON envs(status);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    slug TEXT,
    details TEXT NOT NULL DEFAULT '{}'
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""

_COLUMNS = (
    "id, slug, owner, branch, status, created_at, expires_at, hard_deadline, extensions, "
    "memory_mb, estimated_hourly_cost, failure_reason, updated_at, ended_at, "
    "teardown_attempts, next_attempt_at"
)
_LIVE_SQL = "(" + ",".join(f"'{s.value}'" for s in sorted(LIVE)) + ")"


class SlugConflict(Exception):
    pass


class NotFound(Exception):
    pass


class BadState(Exception):
    def __init__(self, env: Environment, detail: str) -> None:
        super().__init__(detail)
        self.env = env
        self.detail = detail


def _row_to_env(row: sqlite3.Row) -> Environment:
    return Environment(
        id=row["id"],
        slug=row["slug"],
        owner=row["owner"],
        branch=row["branch"],
        status=Status(row["status"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        hard_deadline=row["hard_deadline"],
        extensions=row["extensions"],
        memory_mb=row["memory_mb"],
        estimated_hourly_cost=row["estimated_hourly_cost"],
        failure_reason=row["failure_reason"],
        updated_at=row["updated_at"],
        ended_at=row["ended_at"],
        teardown_attempts=row["teardown_attempts"],
        next_attempt_at=row["next_attempt_at"],
    )


class Store:
    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def ping(self) -> bool:
        with self._lock:
            return bool(self._conn.execute("SELECT 1").fetchone()[0] == 1)

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        ts: float,
        actor: str,
        action: str,
        slug: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit (ts, actor, action, slug, details) VALUES (?, ?, ?, ?, ?)",
            (ts, actor, action, slug, json.dumps(details or {}, sort_keys=True)),
        )

    def audit(
        self,
        ts: float,
        actor: str,
        action: str,
        slug: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._tx() as conn:
            self._audit(conn, ts, actor, action, slug, details)

    def reserve(
        self,
        env: Environment,
        *,
        limits: Limits,
        actor: str,
    ) -> Environment:
        """Quota check + slug reservation + insert, atomically."""
        with self._tx() as conn:
            if conn.execute(
                f"SELECT 1 FROM envs WHERE slug = ? AND status IN {_LIVE_SQL}", (env.slug,)
            ).fetchone():
                raise SlugConflict(env.slug)
            row = conn.execute(
                f"""SELECT
                    COALESCE(SUM(owner = ?), 0) AS owner_live,
                    COUNT(*) AS total_live,
                    COALESCE(SUM(memory_mb), 0) AS reserved_mb,
                    MIN(CASE WHEN owner = ? THEN expires_at END) AS owner_next,
                    MIN(expires_at) AS global_next
                FROM envs WHERE status IN {_LIVE_SQL}""",
                (env.owner, env.owner),
            ).fetchone()
            usage = Usage(
                owner_live=row["owner_live"],
                total_live=row["total_live"],
                reserved_mb=row["reserved_mb"],
                owner_next_expiry=row["owner_next"],
                global_next_expiry=row["global_next"],
            )
            check(limits, usage, request_mb=env.memory_mb, owner=env.owner, now=env.created_at)
            conn.execute(
                f"INSERT INTO envs ({_COLUMNS}) VALUES ({','.join('?' * 16)})",
                (
                    env.id,
                    env.slug,
                    env.owner,
                    env.branch,
                    env.status.value,
                    env.created_at,
                    env.expires_at,
                    env.hard_deadline,
                    env.extensions,
                    env.memory_mb,
                    env.estimated_hourly_cost,
                    env.failure_reason,
                    env.updated_at,
                    env.ended_at,
                    env.teardown_attempts,
                    env.next_attempt_at,
                ),
            )
            self._audit(
                conn,
                env.created_at,
                actor,
                "env.create",
                env.slug,
                {
                    "id": env.id,
                    "owner": env.owner,
                    "branch": env.branch,
                    "ttl_seconds": int(env.expires_at - env.created_at),
                },
            )
        return env

    def get(self, env_id: str) -> Environment:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM envs WHERE id = ?", (env_id,)
            ).fetchone()
        if row is None:
            raise NotFound(env_id)
        return _row_to_env(row)

    def get_by_slug(self, slug: str) -> Environment:
        """The live env for this slug, else the most recent historical one."""
        with self._lock:
            row = self._conn.execute(
                f"""SELECT {_COLUMNS} FROM envs WHERE slug = ?
                    ORDER BY (status IN {_LIVE_SQL}) DESC, created_at DESC LIMIT 1""",
                (slug,),
            ).fetchone()
        if row is None:
            raise NotFound(slug)
        return _row_to_env(row)

    def list_envs(
        self,
        *,
        owner: str | None = None,
        include_terminal: bool = False,
        limit: int = 200,
    ) -> list[Environment]:
        clauses, params = [], []
        if owner is not None:
            clauses.append("owner = ?")
            params.append(owner)
        if not include_terminal:
            clauses.append(f"status IN {_LIVE_SQL}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM envs {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_row_to_env(r) for r in rows]

    def by_status(self, statuses: Iterable[Status]) -> list[Environment]:
        values = [s.value for s in statuses]
        marks = ",".join("?" * len(values))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM envs WHERE status IN ({marks}) ORDER BY created_at",
                values,
            ).fetchall()
        return [_row_to_env(r) for r in rows]

    def transition(
        self,
        env_id: str,
        target: Status,
        *,
        now: float,
        actor: str,
        reason: str | None = None,
        expected: Iterable[Status] | None = None,
        teardown_attempts: int | None = None,
        next_attempt_at: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> Environment:
        """Move an env to ``target``; raises IllegalTransition on a bad edge."""
        with self._tx() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM envs WHERE id = ?", (env_id,)).fetchone()
            if row is None:
                raise NotFound(env_id)
            env = _row_to_env(row)
            if expected is not None and env.status not in set(expected):
                raise BadState(env, f"environment is {env.status}")
            check_transition(env.status, target)
            updated = replace(
                env,
                status=target,
                updated_at=now,
                failure_reason=reason if reason is not None else env.failure_reason,
                ended_at=now if target in (Status.DROPPED, Status.FAILED) else env.ended_at,
                teardown_attempts=(
                    teardown_attempts if teardown_attempts is not None else env.teardown_attempts
                ),
                next_attempt_at=next_attempt_at,
            )
            conn.execute(
                """UPDATE envs SET status = ?, updated_at = ?, failure_reason = ?, ended_at = ?,
                   teardown_attempts = ?, next_attempt_at = ? WHERE id = ?""",
                (
                    updated.status.value,
                    updated.updated_at,
                    updated.failure_reason,
                    updated.ended_at,
                    updated.teardown_attempts,
                    updated.next_attempt_at,
                    env_id,
                ),
            )
            audit_details = {"id": env_id, "from": env.status.value, "to": target.value}
            if reason:
                audit_details["reason"] = reason
            audit_details.update(details or {})
            self._audit(conn, now, actor, f"env.{target.value.lower()}", env.slug, audit_details)
        return updated

    def reset_teardown(self, env_id: str, *, now: float, actor: str) -> Environment:
        """Re-arm retries of a DROP_FAILED env (explicit delete by a human)."""
        with self._tx() as conn:
            env = _row_to_env(
                conn.execute(f"SELECT {_COLUMNS} FROM envs WHERE id = ?", (env_id,)).fetchone()
            )
            conn.execute(
                "UPDATE envs SET teardown_attempts = 0, next_attempt_at = ?, updated_at = ? "
                "WHERE id = ?",
                (now, now, env_id),
            )
            self._audit(conn, now, actor, "env.delete.retry", env.slug, {"id": env_id})
        return replace(env, teardown_attempts=0, next_attempt_at=now, updated_at=now)

    def extend(
        self, env_id: str, *, by_seconds: int, now: float, actor: str
    ) -> tuple[Environment, bool]:
        """Push ``expires_at`` by ``by_seconds``, capped at ``hard_deadline``.

        Returns the env and whether the cap was applied."""
        with self._tx() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM envs WHERE id = ?", (env_id,)).fetchone()
            if row is None:
                raise NotFound(env_id)
            env = _row_to_env(row)
            if env.status is not Status.RUNNING:
                raise BadState(env, f"only RUNNING environments can be extended (is {env.status})")
            wanted = env.expires_at + by_seconds
            new_expiry = min(wanted, env.hard_deadline)
            if new_expiry <= env.expires_at:
                raise BadState(env, "environment already reached its maximum total lifetime")
            conn.execute(
                "UPDATE envs SET expires_at = ?, extensions = extensions + 1, updated_at = ? "
                "WHERE id = ?",
                (new_expiry, now, env_id),
            )
            capped = new_expiry < wanted
            self._audit(
                conn,
                now,
                actor,
                "env.extend",
                env.slug,
                {
                    "id": env_id,
                    "by_seconds": by_seconds,
                    "old_expires_at": env.expires_at,
                    "new_expires_at": new_expiry,
                    "capped": capped,
                },
            )
        return replace(
            env, expires_at=new_expiry, extensions=env.extensions + 1, updated_at=now
        ), capped

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, actor, action, slug, details FROM audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "actor": r["actor"],
                "action": r["action"],
                "slug": r["slug"],
                "details": json.loads(r["details"]),
            }
            for r in rows
        ]

    def counts_by_status(self) -> dict[Status, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM envs GROUP BY status"
            ).fetchall()
        counts = dict.fromkeys(Status, 0)
        for r in rows:
            counts[Status(r["status"])] = r["n"]
        return counts

    def finops(self, now: float) -> dict[str, float | int]:
        with self._lock:
            live = self._conn.execute(
                f"""SELECT COUNT(*) AS n, COALESCE(SUM(memory_mb), 0) AS mb,
                    COALESCE(SUM(estimated_hourly_cost), 0) AS hourly
                    FROM envs WHERE status IN {_LIVE_SQL}"""
            ).fetchone()
            total = self._conn.execute(
                """SELECT COALESCE(SUM(estimated_hourly_cost *
                       (MAX(COALESCE(ended_at, ?), created_at) - created_at) / 3600.0), 0) AS cost,
                       COUNT(*) AS n, MIN(created_at) AS first
                   FROM envs""",
                (now,),
            ).fetchone()
        return {
            "active_envs": live["n"],
            "reserved_memory_mb": live["mb"],
            "estimated_cost_per_hour": round(live["hourly"], 6),
            "estimated_cost_since_start": round(total["cost"], 6),
            "envs_created_total": total["n"],
            "first_env_at": total["first"] or 0,
        }
