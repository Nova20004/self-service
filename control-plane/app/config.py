"""Server configuration, read once from environment variables.

Everything that ends up in a spawned container (images, limits, labels,
commands) comes from here, never from user input.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(ValueError):
    pass


def _int(env: Mapping[str, str], key: str, default: int, minimum: int = 0) -> int:
    raw = env.get(key, "")
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "")
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{key} must be >= 0, got {value}")
    return value


def parse_owner_tokens(raw: str) -> dict[str, str]:
    """Parse ``alice:token,bob:token`` into ``{"alice": "token", ...}``."""
    owners: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        name, sep, token = item.partition(":")
        name = name.strip()
        token = token.strip()
        if not sep or not name or not token:
            raise ConfigError("OWNER_TOKENS entries must look like name:token")
        if name in owners:
            raise ConfigError(f"duplicate owner in OWNER_TOKENS: {name}")
        owners[name] = token
    return owners


@dataclass(frozen=True)
class Settings:
    admin_token: str
    owner_tokens: dict[str, str]
    slack_signing_secret: str

    database_path: str = "/data/state.db"
    docker_host: str = "http://docker-proxy-rw:2375"
    traefik_container: str = "eidp-traefik"
    base_domain: str = "localhost"
    edge_port: int = 8080

    app_image: str = "ephemeral-idp/workload:0.1.0"
    db_image: str = "ephemeral-idp/postgres:17.11-alpine-p1"
    app_memory_mb: int = 64
    db_memory_mb: int = 128
    app_cpus: float = 0.25
    db_cpus: float = 0.5
    app_pids_limit: int = 64
    db_pids_limit: int = 128

    min_ttl_seconds: int = 60
    max_ttl_hours: int = 48
    max_total_lifetime_hours: int = 72
    max_envs_per_owner: int = 3
    max_envs_total: int = 10
    max_total_memory_mb: int = 1024
    cost_per_gb_hour: float = 0.01

    reconcile_interval_seconds: float = 3.0
    provision_timeout_seconds: int = 120
    health_timeout_seconds: int = 60
    orphan_grace_seconds: int = 60
    teardown_max_attempts: int = 5
    teardown_backoff_seconds: float = 2.0
    provision_workers: int = 2

    @property
    def env_memory_mb(self) -> int:
        return self.app_memory_mb + self.db_memory_mb

    @property
    def max_ttl_seconds(self) -> int:
        return self.max_ttl_hours * 3600

    @property
    def max_total_lifetime_seconds(self) -> int:
        return self.max_total_lifetime_hours * 3600

    def env_url(self, slug: str) -> str:
        port = "" if self.edge_port == 80 else f":{self.edge_port}"
        return f"http://{slug}.{self.base_domain}{port}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        admin = env.get("ADMIN_TOKEN", "")
        secret = env.get("SLACK_SIGNING_SECRET", "")
        if len(admin) < 16:
            raise ConfigError("ADMIN_TOKEN must be set (>= 16 chars); run `make env`")
        if len(secret) < 16:
            raise ConfigError("SLACK_SIGNING_SECRET must be set (>= 16 chars)")
        owners = parse_owner_tokens(env.get("OWNER_TOKENS", ""))
        settings = cls(
            admin_token=admin,
            owner_tokens=owners,
            slack_signing_secret=secret,
            database_path=env.get("DATABASE_PATH", cls.database_path),
            docker_host=env.get("DOCKER_HOST_URL", cls.docker_host),
            traefik_container=env.get("TRAEFIK_CONTAINER", cls.traefik_container),
            base_domain=env.get("BASE_DOMAIN", cls.base_domain),
            edge_port=_int(env, "EDGE_PORT", cls.edge_port, 1),
            app_image=env.get("APP_IMAGE", cls.app_image),
            db_image=env.get("DB_IMAGE", cls.db_image),
            app_memory_mb=_int(env, "APP_MEMORY_MB", cls.app_memory_mb, 16),
            db_memory_mb=_int(env, "DB_MEMORY_MB", cls.db_memory_mb, 64),
            app_cpus=_float(env, "APP_CPUS", cls.app_cpus),
            db_cpus=_float(env, "DB_CPUS", cls.db_cpus),
            min_ttl_seconds=_int(env, "MIN_TTL_SECONDS", cls.min_ttl_seconds, 1),
            max_ttl_hours=_int(env, "MAX_TTL_HOURS", cls.max_ttl_hours, 1),
            max_total_lifetime_hours=_int(
                env, "MAX_TOTAL_LIFETIME_HOURS", cls.max_total_lifetime_hours, 1
            ),
            max_envs_per_owner=_int(env, "MAX_ENVS_PER_OWNER", cls.max_envs_per_owner, 1),
            max_envs_total=_int(env, "MAX_ENVS_TOTAL", cls.max_envs_total, 1),
            max_total_memory_mb=_int(env, "MAX_TOTAL_MEMORY_MB", cls.max_total_memory_mb, 1),
            cost_per_gb_hour=_float(env, "COST_PER_GB_HOUR", cls.cost_per_gb_hour),
            reconcile_interval_seconds=_float(
                env, "RECONCILE_INTERVAL_SECONDS", cls.reconcile_interval_seconds
            ),
            provision_timeout_seconds=_int(
                env, "PROVISION_TIMEOUT_SECONDS", cls.provision_timeout_seconds, 5
            ),
            health_timeout_seconds=_int(
                env, "HEALTH_TIMEOUT_SECONDS", cls.health_timeout_seconds, 1
            ),
            orphan_grace_seconds=_int(env, "ORPHAN_GRACE_SECONDS", cls.orphan_grace_seconds),
            teardown_max_attempts=_int(env, "TEARDOWN_MAX_ATTEMPTS", cls.teardown_max_attempts, 1),
        )
        if settings.max_ttl_seconds < settings.min_ttl_seconds:
            raise ConfigError("MAX_TTL_HOURS must be larger than MIN_TTL_SECONDS")
        if settings.max_total_lifetime_hours < settings.max_ttl_hours:
            raise ConfigError("MAX_TOTAL_LIFETIME_HOURS must be >= MAX_TTL_HOURS")
        return settings
