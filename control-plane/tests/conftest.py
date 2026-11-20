from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Executor, Future
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import Platform, build_platform, create_app
from app.models import FakeClock
from app.provisioner import InlineExecutor
from app.runtime import FakeRuntime
from app.store import Store

ADMIN = "admin-token-0123456789abcdef"
ALICE = "alice-token-0123456789abcdef"
BOB = "bob-token-0123456789abcdef012"
SECRET = "slack-secret-0123456789abcdef"


class ParkedExecutor(Executor):
    """Accepts work and never runs it: simulates a worker lost in a crash."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        return Future()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        admin_token=ADMIN,
        owner_tokens={"alice": ALICE, "bob": BOB},
        slack_signing_secret=SECRET,
        database_path=":memory:",
        min_ttl_seconds=10,
        orphan_grace_seconds=30,
        health_timeout_seconds=5,
        provision_timeout_seconds=60,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def runtime(clock: FakeClock) -> FakeRuntime:
    return FakeRuntime(clock)


@pytest.fixture
def make_platform(settings: Settings, clock: FakeClock, runtime: FakeRuntime):
    stores: list[Store] = []

    def _make(store: Store | None = None, **overrides: Any) -> Platform:
        s = replace(settings, **overrides) if overrides else settings
        store = store or Store(":memory:")
        stores.append(store)
        return build_platform(
            s,
            store=store,
            runtime=runtime,
            clock=clock,
            executor=InlineExecutor(),
        )

    yield _make
    for store in stores:
        store.close()


@pytest.fixture
def platform(make_platform) -> Platform:
    p: Platform = make_platform()
    return p


@pytest.fixture
def client(platform: Platform) -> Iterator[TestClient]:
    with TestClient(create_app(platform, background=False)) as c:
        yield c
