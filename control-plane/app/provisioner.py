"""Environment provisioning and teardown.

``request`` runs on the API thread: it validates input, reserves the slug and
quota in one ``BEGIN IMMEDIATE`` transaction and hands the work to a
background executor. ``provision`` runs on the worker; any failing step
triggers an idempotent rollback and leaves the env FAILED.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .metrics import Metrics
from .models import Clock, Environment, IllegalTransition, Status, iso
from .quota import Limits, QuotaExceeded
from .runtime import (
    ENV_ID_LABEL,
    EXPIRES_LABEL,
    MANAGED_BY_KEY,
    MANAGED_BY_VALUE,
    OWNER_LABEL,
    ROLE_LABEL,
    SLUG_LABEL,
    ContainerSpec,
    HealthCheck,
    Runtime,
)
from .slug import slugify
from .store import Store
from .ttl import parse_ttl

log = logging.getLogger(__name__)

DB_USER = "app"
DB_NAME = "app"
APP_PORT = 8000
# postgres:*-alpine runs as uid/gid 70. Running it directly as that user with
# no capabilities at all was verified empirically (see docs/THREAT-MODEL.md).
POSTGRES_UID = "70:70"
WORKLOAD_UID = "10001:10001"
# Sized for a 128 MB cgroup: ~16 MB shared buffers, few connections.
POSTGRES_FLAGS = [
    "-c",
    "shared_buffers=16MB",
    "-c",
    "max_connections=20",
    "-c",
    "work_mem=1MB",
    "-c",
    "maintenance_work_mem=16MB",
    "-c",
    "wal_buffers=1MB",
    "-c",
    "effective_cache_size=48MB",
    "-c",
    "max_wal_size=128MB",
    "-c",
    "fsync=on",
]


class ProvisionError(Exception):
    pass


def network_name(env_id: str) -> str:
    return f"eidp-env-{env_id}"


def app_name(env_id: str) -> str:
    return f"eidp-{env_id}-app"


def db_name(env_id: str) -> str:
    return f"eidp-{env_id}-db"


def volume_name(env_id: str) -> str:
    return f"eidp-{env_id}-pgdata"


@dataclass(frozen=True)
class CreateResult:
    env: Environment
    dsn: str
    url: str


class InlineExecutor(Executor):
    """Runs work synchronously; used by tests instead of a thread pool."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # pragma: no cover - surfaced through the future
            future.set_exception(exc)
        return future


class Provisioner:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        runtime: Runtime,
        clock: Clock,
        metrics: Metrics,
        executor: Executor,
    ) -> None:
        self.settings = settings
        self.store = store
        self.runtime = runtime
        self.clock = clock
        self.metrics = metrics
        self.executor = executor
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    @property
    def limits(self) -> Limits:
        s = self.settings
        return Limits(s.max_envs_per_owner, s.max_envs_total, s.max_total_memory_mb)

    def inflight(self) -> set[str]:
        with self._lock:
            return set(self._inflight)

    def labels(self, env: Environment, role: str) -> dict[str, str]:
        return {
            MANAGED_BY_KEY: MANAGED_BY_VALUE,
            ENV_ID_LABEL: env.id,
            OWNER_LABEL: env.owner,
            SLUG_LABEL: env.slug,
            EXPIRES_LABEL: iso(env.expires_at) or "",
            ROLE_LABEL: role,
        }

    def request(self, *, owner: str, branch: str, ttl: str, actor: str) -> CreateResult:
        """Validate and reserve; raises SlugError, TTLError, QuotaExceeded, SlugConflict."""
        s = self.settings
        slug = slugify(branch)
        ttl_seconds = parse_ttl(ttl, min_seconds=s.min_ttl_seconds, max_seconds=s.max_ttl_seconds)
        now = self.clock.now()
        memory = s.env_memory_mb
        env = Environment(
            id=secrets.token_hex(4),
            slug=slug,
            owner=owner,
            branch=branch,
            status=Status.PENDING,
            created_at=now,
            expires_at=now + ttl_seconds,
            hard_deadline=now + s.max_total_lifetime_seconds,
            extensions=0,
            memory_mb=memory,
            estimated_hourly_cost=memory / 1024 * s.cost_per_gb_hour,
            failure_reason=None,
            updated_at=now,
        )
        # Registered before the row exists so the reconciler never mistakes a
        # fresh reservation for one abandoned by a crashed process.
        with self._lock:
            self._inflight.add(env.id)
        try:
            self.store.reserve(env, limits=self.limits, actor=actor)
        except QuotaExceeded as exc:
            self._forget(env.id)
            self.metrics.quota_rejections.labels(reason=exc.reason).inc()
            self.store.audit(
                now, actor, "env.create.rejected", slug, {"reason": exc.reason, "owner": owner}
            )
            raise
        except Exception:
            self._forget(env.id)
            raise
        self.metrics.envs_created.inc()
        env = self.store.transition(env.id, Status.PROVISIONING, now=now, actor="provisioner")
        password = secrets.token_urlsafe(24)
        self.executor.submit(self.provision, env.id, password)
        dsn = f"postgresql://{DB_USER}:{password}@db:5432/{DB_NAME}"
        return CreateResult(env=env, dsn=dsn, url=s.env_url(slug))

    def provision(self, env_id: str, password: str) -> None:
        try:
            self._provision(env_id, password)
        except Exception:  # pragma: no cover - last-resort guard for the worker thread
            log.exception("provision worker crashed", extra={"env_id": env_id})
        finally:
            self._forget(env_id)

    def _forget(self, env_id: str) -> None:
        with self._lock:
            self._inflight.discard(env_id)

    def _provision(self, env_id: str, password: str) -> None:
        env = self.store.get(env_id)
        started = env.created_at
        if env.status is not Status.PROVISIONING:
            return
        step = "init"
        try:
            step = "images"
            for ref in (self.settings.db_image, self.settings.app_image):
                if not self.runtime.image_exists(ref):
                    raise ProvisionError(f"image {ref} is not present locally; run `make up`")
            step = "network"
            self.runtime.create_network(network_name(env.id), self.labels(env, "network"))
            step = "volume"
            self.runtime.create_volume(volume_name(env.id), self.labels(env, "volume"))
            step = "db"
            self.runtime.run_container(self._db_spec(env, password))
            self._wait_healthy(db_name(env.id), "db")
            step = "app"
            self.runtime.run_container(self._app_spec(env, password))
            step = "route"
            self.runtime.connect(network_name(env.id), self.settings.traefik_container)
            step = "app-health"
            self._wait_healthy(app_name(env.id), "app")
        except Exception as exc:
            reason = f"{step}: {exc}"[:300]
            log.warning(
                "provisioning failed, rolling back", extra={"env_id": env.id, "reason": reason}
            )
            self.fail(env, reason)
            return
        try:
            self.store.transition(env.id, Status.RUNNING, now=self.clock.now(), actor="provisioner")
        except IllegalTransition:
            # The reconciler timed us out meanwhile; make sure nothing leaks.
            self.teardown(env)
            return
        self.metrics.provision_duration.observe(self.clock.now() - started)

    def fail(self, env: Environment, reason: str) -> None:
        """Roll back whatever exists and mark the env FAILED (idempotent)."""
        try:
            self.teardown(env)
        except Exception as exc:
            reason = f"{reason}; rollback incomplete: {exc}"[:300]
        self.metrics.provision_failures.inc()
        current = self.store.get(env.id)
        if current.status in (Status.PENDING, Status.PROVISIONING):
            self.store.transition(
                env.id, Status.FAILED, now=self.clock.now(), actor="provisioner", reason=reason
            )

    def teardown(self, env: Environment) -> None:
        """Remove everything an env owns, in dependency order. Each step
        tolerates missing resources, so the whole call is idempotent."""
        self.runtime.remove_container(app_name(env.id))
        self.runtime.remove_container(db_name(env.id))
        self.runtime.disconnect(network_name(env.id), self.settings.traefik_container)
        self.runtime.remove_network(network_name(env.id))
        self.runtime.remove_volume(volume_name(env.id))

    def _wait_healthy(self, container: str, role: str) -> None:
        deadline = self.clock.now() + self.settings.health_timeout_seconds
        while True:
            state = self.runtime.container_state(container)
            if state is None:
                raise ProvisionError(f"{role} container disappeared")
            if state.status in ("exited", "dead"):
                tail = self.runtime.container_logs(container, tail=5).strip().splitlines()
                last = tail[-1] if tail else ""
                raise ProvisionError(f"{role} container exited ({state.exit_code}) {last}")
            if state.health == "healthy":
                return
            if state.health == "unhealthy":
                raise ProvisionError(f"{role} container is unhealthy")
            if self.clock.now() >= deadline:
                raise ProvisionError(
                    f"{role} not healthy after {self.settings.health_timeout_seconds}s"
                )
            self.clock.sleep(0.5)

    def _db_spec(self, env: Environment, password: str) -> ContainerSpec:
        s = self.settings
        return ContainerSpec(
            name=db_name(env.id),
            image=s.db_image,
            network=network_name(env.id),
            aliases=["db"],
            labels=self.labels(env, "db"),
            env={"POSTGRES_USER": DB_USER, "POSTGRES_DB": DB_NAME, "POSTGRES_PASSWORD": password},
            command=["postgres", *POSTGRES_FLAGS],
            user=POSTGRES_UID,
            memory_mb=s.db_memory_mb,
            cpus=s.db_cpus,
            pids_limit=s.db_pids_limit,
            read_only=True,
            tmpfs={
                "/var/run/postgresql": "rw,noexec,nosuid,size=4m,mode=1777",
                "/tmp": "rw,noexec,nosuid,size=16m,mode=1777",
            },
            volumes={volume_name(env.id): "/var/lib/postgresql/data"},
            healthcheck=HealthCheck(
                test=["CMD", "pg_isready", "-U", DB_USER, "-d", DB_NAME, "-h", "127.0.0.1"],
            ),
        )

    def _app_spec(self, env: Environment, password: str) -> ContainerSpec:
        s = self.settings
        router = f"env-{env.id}"
        labels = self.labels(env, "app")
        labels.update(
            {
                "traefik.enable": "true",
                "traefik.docker.network": network_name(env.id),
                f"traefik.http.routers.{router}.rule": f"Host(`{env.slug}.{s.base_domain}`)",
                f"traefik.http.routers.{router}.entrypoints": "web",
                f"traefik.http.routers.{router}.service": router,
                f"traefik.http.services.{router}.loadbalancer.server.port": str(APP_PORT),
            }
        )
        return ContainerSpec(
            name=app_name(env.id),
            image=s.app_image,
            network=network_name(env.id),
            labels=labels,
            env={
                "BRANCH": env.branch,
                "ENV_ID": env.id,
                "ENV_SLUG": env.slug,
                "OWNER": env.owner,
                "EXPIRES_AT": iso(env.expires_at) or "",
                "DATABASE_URL": f"postgresql://{DB_USER}:{password}@db:5432/{DB_NAME}",
                "PORT": str(APP_PORT),
            },
            user=WORKLOAD_UID,
            memory_mb=s.app_memory_mb,
            cpus=s.app_cpus,
            pids_limit=s.app_pids_limit,
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=8m"},
            healthcheck=HealthCheck(
                test=[
                    "CMD",
                    "python",
                    "-c",
                    "import urllib.request;"
                    f"urllib.request.urlopen('http://127.0.0.1:{APP_PORT}/healthz', timeout=2)",
                ],
            ),
        )
