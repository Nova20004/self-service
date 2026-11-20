"""Domain model: environment record, lifecycle state machine and clock."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class Status(StrEnum):
    PENDING = "PENDING"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    EXPIRING = "EXPIRING"
    DROPPED = "DROPPED"
    FAILED = "FAILED"
    DROP_FAILED = "DROP_FAILED"


TERMINAL: frozenset[Status] = frozenset({Status.DROPPED, Status.FAILED})
LIVE: frozenset[Status] = frozenset(set(Status) - TERMINAL)

# PENDING -> FAILED covers a control plane that crashed before the worker
# picked the reservation up. DROP_FAILED -> DROP_FAILED is a failed retry.
TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.PENDING: frozenset({Status.PROVISIONING, Status.FAILED}),
    Status.PROVISIONING: frozenset({Status.RUNNING, Status.FAILED}),
    Status.RUNNING: frozenset({Status.EXPIRING}),
    Status.EXPIRING: frozenset({Status.DROPPED, Status.DROP_FAILED}),
    Status.DROP_FAILED: frozenset({Status.DROPPED, Status.DROP_FAILED}),
    Status.DROPPED: frozenset(),
    Status.FAILED: frozenset(),
}


class IllegalTransition(RuntimeError):
    def __init__(self, current: Status, target: Status) -> None:
        super().__init__(f"illegal transition {current} -> {target}")
        self.current = current
        self.target = target


def check_transition(current: Status, target: Status) -> None:
    if target not in TRANSITIONS[current]:
        raise IllegalTransition(current, target)


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Environment:
    id: str
    slug: str
    owner: str
    branch: str
    status: Status
    created_at: float
    expires_at: float
    hard_deadline: float
    extensions: int
    memory_mb: int
    estimated_hourly_cost: float
    failure_reason: str | None
    updated_at: float
    ended_at: float | None = None
    teardown_attempts: int = 0
    next_attempt_at: float | None = None

    @property
    def live(self) -> bool:
        return self.status in LIVE

    def to_public(self, url: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "owner": self.owner,
            "branch": self.branch,
            "status": self.status.value,
            "url": url,
            "created_at": iso(self.created_at),
            "expires_at": iso(self.expires_at),
            "hard_deadline": iso(self.hard_deadline),
            "extensions": self.extensions,
            "memory_mb": self.memory_mb,
            "estimated_hourly_cost": round(self.estimated_hourly_cost, 6),
            "failure_reason": self.failure_reason,
        }


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """Deterministic clock for tests: ``sleep`` advances time instantly."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds

    def advance(self, seconds: float) -> None:
        self._now += seconds
