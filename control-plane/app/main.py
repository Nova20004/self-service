"""FastAPI application: REST API, Slack slash command, health and metrics."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import AsyncIterator
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api_errors import (
    ApiError,
    api_error_handler,
    http_error_handler,
    problem,
    validation_error_handler,
)
from .auth import Principal, TokenAuthenticator
from .config import Settings
from .metrics import Metrics
from .models import Clock, Environment, IllegalTransition, Status, SystemClock, iso
from .provisioner import Provisioner, db_name
from .quota import QuotaExceeded
from .reconciler import Reconciler
from .runtime import DockerRuntime, Runtime
from .slack import SignatureError, SlackVerifier, ephemeral, parse_command_text, parse_form
from .slug import SlugError, sanitize_owner
from .store import BadState, NotFound, SlugConflict, Store
from .ttl import TTLError, parse_duration

log = logging.getLogger("control_plane")


class JsonFormatter(logging.Formatter):
    _STD = set(vars(logging.makeLogRecord({})))

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in self._STD and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs every Docker API call at INFO: three lines per reconcile pass.
    logging.getLogger("httpx").setLevel(logging.WARNING)


class CreateEnvRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=255)
    ttl: str = Field(default="24h", max_length=16)


class ExtendRequest(BaseModel):
    by: str = Field(min_length=2, max_length=16)


def get_principal(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    platform: Platform = request.app.state.platform
    return platform.auth.authenticate(authorization)


Auth = Annotated[Principal, Depends(get_principal)]


@dataclass
class Platform:
    settings: Settings
    store: Store
    runtime: Runtime
    clock: Clock
    metrics: Metrics
    provisioner: Provisioner
    reconciler: Reconciler
    auth: TokenAuthenticator
    slack: SlackVerifier


def build_platform(
    settings: Settings,
    *,
    store: Store | None = None,
    runtime: Runtime | None = None,
    clock: Clock | None = None,
    executor: Executor | None = None,
    metrics: Metrics | None = None,
) -> Platform:
    store = store or Store(settings.database_path)
    runtime = runtime or DockerRuntime(settings.docker_host)
    clock = clock or SystemClock()
    metrics = metrics or Metrics()
    executor = executor or ThreadPoolExecutor(
        max_workers=settings.provision_workers, thread_name_prefix="provision"
    )
    provisioner = Provisioner(settings, store, runtime, clock, metrics, executor)
    reconciler = Reconciler(settings, store, runtime, provisioner, clock, metrics)
    return Platform(
        settings=settings,
        store=store,
        runtime=runtime,
        clock=clock,
        metrics=metrics,
        provisioner=provisioner,
        reconciler=reconciler,
        auth=TokenAuthenticator(settings.admin_token, settings.owner_tokens),
        slack=SlackVerifier(settings.slack_signing_secret),
    )


def create_app(platform: Platform, *, background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if background:
            platform.reconciler.start()
        yield
        if background:
            platform.reconciler.stop()

    app = FastAPI(
        title="ephemeral-idp control plane",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    app.state.platform = platform
    p = platform

    def public(env: Environment) -> dict[str, Any]:
        data = env.to_public(p.settings.env_url(env.slug))
        data["db_container"] = db_name(env.id)
        return data

    def load(slug: str, who: Principal) -> Environment:
        try:
            env = p.store.get_by_slug(slug)
        except NotFound:
            raise ApiError(404, f"environment {slug!r} not found", code="not-found") from None
        if not who.can_access(env):
            raise ApiError(403, f"environment {slug!r} belongs to another owner", code="not-owner")
        return env

    def create(owner: str, branch: str, ttl: str, actor: str) -> dict[str, Any]:
        try:
            result = p.provisioner.request(owner=owner, branch=branch, ttl=ttl, actor=actor)
        except QuotaExceeded as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
            raise ApiError(
                429,
                exc.detail,
                code=f"quota-{exc.reason.replace('_', '-')}",
                headers=headers,
                extra={"reason": exc.reason},
            ) from None
        except SlugConflict:
            raise ApiError(
                409, "a live environment already uses this branch slug", code="slug-conflict"
            ) from None
        except (SlugError, TTLError) as exc:
            raise ApiError(400, str(exc), code="validation-error") from None
        body = public(result.env)
        body["dsn"] = result.dsn
        body["dsn_note"] = (
            "shown once and never stored; reachable from inside the "
            "environment network as host 'db'"
        )
        return body

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        checks = {"database": False, "docker": False}
        try:
            checks["database"] = p.store.ping()
        except Exception:
            checks["database"] = False
        checks["docker"] = p.runtime.ping()
        ready = all(checks.values())
        return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(p.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/envs", status_code=202)
    def create_env(req: CreateEnvRequest, who: Auth) -> dict[str, Any]:
        return create(who.name, req.branch, req.ttl, who.name)

    @app.get("/v1/envs")
    def list_envs(
        who: Auth, all: Annotated[bool, Query(description="include terminal states")] = False
    ) -> dict[str, Any]:
        envs = p.store.list_envs(owner=None if who.is_admin else who.name, include_terminal=all)
        return {"items": [public(e) for e in envs], "count": len(envs)}

    @app.get("/v1/envs/{slug}")
    def get_env(slug: str, who: Auth) -> dict[str, Any]:
        return public(load(slug, who))

    @app.post("/v1/envs/{slug}/extend")
    def extend_env(slug: str, req: ExtendRequest, who: Auth) -> dict[str, Any]:
        env = load(slug, who)
        try:
            seconds = parse_duration(req.by)
        except TTLError as exc:
            raise ApiError(400, str(exc), code="validation-error") from None
        try:
            updated, capped = p.store.extend(
                env.id, by_seconds=seconds, now=p.clock.now(), actor=who.name
            )
        except BadState as exc:
            raise ApiError(409, exc.detail, code="bad-state") from None
        body = public(updated)
        body["previous_expires_at"] = iso(env.expires_at)
        body["capped_at_hard_deadline"] = capped
        return body

    @app.delete("/v1/envs/{slug}", status_code=202)
    def delete_env(slug: str, who: Auth) -> dict[str, Any]:
        env = load(slug, who)
        now = p.clock.now()
        if env.status is Status.RUNNING:
            try:
                env = p.store.transition(
                    env.id,
                    Status.EXPIRING,
                    now=now,
                    actor=who.name,
                    reason=f"deleted by {who.name}",
                    expected=[Status.RUNNING],
                    next_attempt_at=now,
                )
            except (BadState, IllegalTransition):
                env = p.store.get(env.id)
        elif env.status is Status.DROP_FAILED:
            env = p.store.reset_teardown(env.id, now=now, actor=who.name)
        elif env.status in (Status.PENDING, Status.PROVISIONING):
            raise ApiError(
                409, "environment is still provisioning; retry once RUNNING", code="bad-state"
            )
        return public(env)

    @app.get("/v1/audit")
    def audit(who: Auth, limit: Annotated[int, Query(ge=1, le=1000)] = 50) -> dict[str, Any]:
        if not who.is_admin:
            raise ApiError(403, "audit log requires the admin token", code="admin-only")
        entries = p.store.list_audit(limit)
        for e in entries:
            e["ts"] = iso(e["ts"])
        return {"items": entries, "count": len(entries)}

    @app.get("/v1/finops/summary")
    def finops(who: Auth) -> dict[str, Any]:
        if not who.is_admin:
            raise ApiError(403, "finops summary requires the admin token", code="admin-only")
        now = p.clock.now()
        summary: dict[str, Any] = dict(p.store.finops(now))
        summary["first_env_at"] = (
            iso(float(summary["first_env_at"])) if summary["first_env_at"] else None
        )
        summary.update(
            {
                "cost_per_gb_hour": p.settings.cost_per_gb_hour,
                "memory_budget_mb": p.settings.max_total_memory_mb,
                "unit": "illustrative cost units (estimate, not billing)",
                "per_env_memory_mb": p.settings.env_memory_mb,
            }
        )
        return summary

    @app.post("/v1/chatops/create-env")
    async def chatops_create(request: Request) -> JSONResponse:
        raw = await request.body()
        try:
            p.slack.verify(
                timestamp=request.headers.get("x-slack-request-timestamp"),
                signature=request.headers.get("x-slack-signature"),
                body=raw,
                now=p.clock.now(),
            )
        except SignatureError as exc:
            p.store.audit(p.clock.now(), "slack", "chatops.rejected", None, {"reason": str(exc)})
            return problem(
                401,
                f"invalid Slack signature: {exc}",
                instance=request.url.path,
                code="bad-signature",
            )
        try:
            form = parse_form(raw)
            args = parse_command_text(form.get("text", ""))
            owner = sanitize_owner(form.get("user_name", ""))
            branch = args.get("branch", "")
            if not branch:
                raise ValueError("usage: /create-env branch=<name> [ttl=24h]")
            body = create(owner, branch, args.get("ttl", "24h"), f"slack:{owner}")
        except ApiError as exc:
            return JSONResponse(ephemeral(f":x: {exc.detail}"))
        except (ValueError, UnicodeDecodeError) as exc:
            return JSONResponse(ephemeral(f":x: {exc}"))
        text = (
            f":rocket: Environment *{body['slug']}* is {body['status']}\n"
            f"URL: {body['url']}\n"
            f"DSN (shown once): `{body['dsn']}`\n"
            f"Expires: {body['expires_at']}"
        )
        return JSONResponse(
            ephemeral(
                text,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
            )
            | {"env": {k: body[k] for k in ("slug", "status", "url", "expires_at", "dsn")}}
        )

    return app


def build_app_from_env() -> FastAPI:  # pragma: no cover - exercised by the e2e job
    configure_logging()
    settings = Settings.from_env()
    platform = build_platform(settings)
    log.info(
        "control plane starting",
        extra={"version": __version__, "owners": sorted(settings.owner_tokens)},
    )
    return create_app(platform)
