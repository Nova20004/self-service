"""Admission quotas, evaluated inside the reservation transaction."""

from __future__ import annotations

from dataclasses import dataclass


class QuotaExceeded(Exception):
    def __init__(self, reason: str, detail: str, retry_after: int | None) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.retry_after = retry_after


@dataclass(frozen=True)
class Usage:
    owner_live: int
    total_live: int
    reserved_mb: int
    owner_next_expiry: float | None
    global_next_expiry: float | None


@dataclass(frozen=True)
class Limits:
    max_envs_per_owner: int
    max_envs_total: int
    max_total_memory_mb: int


def _retry_after(next_expiry: float | None, now: float) -> int | None:
    if next_expiry is None:
        return None
    return max(1, int(next_expiry - now) + 1)


def check(limits: Limits, usage: Usage, *, request_mb: int, owner: str, now: float) -> None:
    if usage.owner_live >= limits.max_envs_per_owner:
        raise QuotaExceeded(
            "per_owner",
            f"{owner} already has {usage.owner_live} live environments "
            f"(limit {limits.max_envs_per_owner}); delete one or wait for expiry",
            _retry_after(usage.owner_next_expiry, now),
        )
    if usage.total_live >= limits.max_envs_total:
        raise QuotaExceeded(
            "total",
            f"platform limit of {limits.max_envs_total} live environments reached",
            _retry_after(usage.global_next_expiry, now),
        )
    if usage.reserved_mb + request_mb > limits.max_total_memory_mb:
        raise QuotaExceeded(
            "memory",
            f"reserving {request_mb} MB would exceed the platform memory budget "
            f"({usage.reserved_mb}/{limits.max_total_memory_mb} MB in use)",
            _retry_after(usage.global_next_expiry, now),
        )
