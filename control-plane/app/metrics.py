"""Prometheus metrics. No per-slug or per-owner labels: cardinality stays
bounded no matter how many environments are created."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from .models import LIVE, Status


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.envs_active = Gauge(
            "ephemeral_envs_active", "Environments by lifecycle status", ["status"], registry=r
        )
        self.envs_created = Counter(
            "ephemeral_envs_created_total", "Environments accepted for creation", registry=r
        )
        self.envs_reaped = Counter(
            "ephemeral_envs_reaped_total",
            "Environments torn down (TTL, delete or drift)",
            registry=r,
        )
        self.quota_rejections = Counter(
            "ephemeral_quota_rejections_total",
            "Create requests rejected by quota",
            ["reason"],
            registry=r,
        )
        self.provision_failures = Counter(
            "ephemeral_provision_failures_total", "Provisioning attempts rolled back", registry=r
        )
        self.orphans_reaped = Counter(
            "ephemeral_orphans_reaped_total",
            "Labelled resources removed without a live record",
            registry=r,
        )
        self.teardown_giveups = Counter(
            "ephemeral_teardown_giveups_total",
            "Teardowns that exhausted their retry budget",
            registry=r,
        )
        self.provision_duration = Histogram(
            "ephemeral_provision_duration_seconds",
            "Time from PENDING to RUNNING",
            buckets=(1, 2, 5, 10, 20, 30, 60, 120),
            registry=r,
        )
        self.reconcile_duration = Histogram(
            "ephemeral_reconcile_duration_seconds",
            "Duration of one reconcile pass",
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
            registry=r,
        )
        self.cost_units = Counter(
            "ephemeral_cost_units_total",
            "Illustrative cost units accrued by live environments (estimate, not billing)",
            registry=r,
        )
        for status in sorted(LIVE):
            self.envs_active.labels(status=status.value).set(0)
        for reason in ("per_owner", "total", "memory"):
            self.quota_rejections.labels(reason=reason)

    def set_active(self, counts: dict[Status, int]) -> None:
        """Only live states: terminal rows accumulate forever and are not "active"."""
        for status, value in counts.items():
            if status in LIVE:
                self.envs_active.labels(status=status.value).set(value)
