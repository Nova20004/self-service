"""Polling reconciler: converge actual Docker state to the desired state in
SQLite. Runs once at startup (crash recovery) and then every
``RECONCILE_INTERVAL_SECONDS``. It only ever touches resources labelled
``managed-by=ephemeral-idp``."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from .config import Settings
from .metrics import Metrics
from .models import Clock, Environment, IllegalTransition, Status
from .provisioner import Provisioner, app_name, db_name
from .runtime import ManagedResource, Runtime, RuntimeError_
from .store import BadState, Store

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 60.0
_KIND_ORDER = {"container": 0, "network": 1, "volume": 2}


@dataclass
class ReconcileReport:
    expired: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    drop_failed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    drifted: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Reconciler:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        runtime: Runtime,
        provisioner: Provisioner,
        clock: Clock,
        metrics: Metrics,
    ) -> None:
        self.settings = settings
        self.store = store
        self.runtime = runtime
        self.provisioner = provisioner
        self.clock = clock
        self.metrics = metrics
        self._last_tick: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.passes = 0

    def run_once(self) -> ReconcileReport:
        report = ReconcileReport()
        with self.metrics.reconcile_duration.time():
            now = self.clock.now()
            self._expire(now, report)
            self._stuck_provisioning(now, report)
            self._drift(now, report)
            self._teardowns(report)
            self._orphans(report)
            self._account(now)
        self.passes += 1
        return report

    def _expire(self, now: float, report: ReconcileReport) -> None:
        for env in self.store.by_status([Status.RUNNING]):
            if env.expires_at <= now:
                try:
                    self.store.transition(
                        env.id,
                        Status.EXPIRING,
                        now=now,
                        actor="reaper",
                        reason="ttl expired",
                        expected=[Status.RUNNING],
                        next_attempt_at=now,
                    )
                    report.expired.append(env.slug)
                except (IllegalTransition, BadState):
                    continue

    def _stuck_provisioning(self, now: float, report: ReconcileReport) -> None:
        inflight = self.provisioner.inflight()
        timeout = self.settings.provision_timeout_seconds
        for env in self.store.by_status([Status.PENDING, Status.PROVISIONING]):
            if env.id not in inflight:
                reason = "control plane restarted during provisioning"
            elif now - env.created_at > timeout:
                reason = f"provisioning exceeded {timeout}s"
            else:
                continue
            log.warning("failing stuck environment", extra={"env_id": env.id, "reason": reason})
            self.provisioner.fail(env, reason)
            report.failed.append(env.slug)

    def _drift(self, now: float, report: ReconcileReport) -> None:
        for env in self.store.by_status([Status.RUNNING]):
            try:
                missing = [
                    role
                    for role, name in (("app", app_name(env.id)), ("db", db_name(env.id)))
                    if (state := self.runtime.container_state(name)) is None
                    or state.status in ("exited", "dead")
                ]
            except RuntimeError_ as exc:
                report.errors.append(f"drift check {env.slug}: {exc}")
                continue
            if not missing:
                continue
            reason = f"containers vanished or stopped: {', '.join(missing)}"
            try:
                self.store.transition(
                    env.id,
                    Status.EXPIRING,
                    now=now,
                    actor="reconciler",
                    reason=reason,
                    expected=[Status.RUNNING],
                    next_attempt_at=now,
                )
                report.drifted.append(env.slug)
            except (IllegalTransition, BadState):
                continue

    def _backoff(self, attempts: int) -> float:
        return float(
            min(self.settings.teardown_backoff_seconds * 2 ** (attempts - 1), MAX_BACKOFF_SECONDS)
        )

    def _teardowns(self, report: ReconcileReport) -> None:
        max_attempts = self.settings.teardown_max_attempts
        for env in self.store.by_status([Status.EXPIRING, Status.DROP_FAILED]):
            now = self.clock.now()
            if env.status is Status.DROP_FAILED and env.teardown_attempts >= max_attempts:
                continue
            if env.next_attempt_at is not None and now < env.next_attempt_at:
                continue
            self._teardown_one(env, now, report)

    def _teardown_one(self, env: Environment, now: float, report: ReconcileReport) -> None:
        try:
            self.provisioner.teardown(env)
        except Exception as exc:
            attempts = env.teardown_attempts + 1
            gave_up = attempts >= self.settings.teardown_max_attempts
            reason = f"teardown attempt {attempts} failed: {exc}"[:300]
            self.store.transition(
                env.id,
                Status.DROP_FAILED,
                now=now,
                actor="reaper",
                reason=reason,
                teardown_attempts=attempts,
                next_attempt_at=None if gave_up else now + self._backoff(attempts),
            )
            report.drop_failed.append(env.slug)
            if gave_up:
                self.metrics.teardown_giveups.inc()
                log.error(
                    "teardown gave up; manual cleanup or DELETE needed",
                    extra={"env_id": env.id, "attempts": attempts},
                )
            return
        self.store.transition(
            env.id, Status.DROPPED, now=now, actor="reaper", teardown_attempts=env.teardown_attempts
        )
        self.metrics.envs_reaped.inc()
        report.dropped.append(env.slug)

    def _orphans(self, report: ReconcileReport) -> None:
        try:
            resources = self.runtime.list_managed()
        except RuntimeError_ as exc:
            report.errors.append(f"list managed resources: {exc}")
            return
        now = self.clock.now()
        live_ids = {e.id for e in self.store.list_envs(include_terminal=False, limit=10_000)}
        grace = self.settings.orphan_grace_seconds
        orphans: list[ManagedResource] = [
            r
            for r in resources
            if (r.env_id is None or r.env_id not in live_ids) and now - r.created_at >= grace
        ]
        for res in sorted(orphans, key=lambda r: _KIND_ORDER[r.kind]):
            try:
                if res.kind == "container":
                    self.runtime.remove_container(res.name)
                elif res.kind == "network":
                    self.runtime.disconnect(res.name, self.settings.traefik_container)
                    self.runtime.remove_network(res.name)
                else:
                    self.runtime.remove_volume(res.name)
            except RuntimeError_ as exc:
                report.errors.append(f"orphan {res.kind} {res.name}: {exc}")
                continue
            self.metrics.orphans_reaped.inc()
            report.orphans.append(f"{res.kind}:{res.name}")
            self.store.audit(
                now,
                "reconciler",
                "orphan.remove",
                None,
                {"kind": res.kind, "name": res.name, "env_id": res.env_id},
            )

    def _account(self, now: float) -> None:
        counts = self.store.counts_by_status()
        self.metrics.set_active(counts)
        if self._last_tick is not None and now > self._last_tick:
            hourly = float(self.store.finops(now)["estimated_cost_per_hour"])
            self.metrics.cost_units.inc(hourly * (now - self._last_tick) / 3600.0)
        self._last_tick = now

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="reconciler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while True:
            try:
                report = self.run_once()
                if report.errors:
                    log.warning("reconcile pass had errors", extra={"errors": report.errors})
            except Exception:
                log.exception("reconcile pass crashed")
            if self._stop.wait(self.settings.reconcile_interval_seconds):
                return
