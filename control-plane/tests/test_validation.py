from __future__ import annotations

import pytest

from app.config import ConfigError, Settings, parse_owner_tokens
from app.slug import RESERVED, SlugError, sanitize_owner, slugify
from app.ttl import TTLError, parse_duration, parse_ttl


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("feature/Login", "feature-login"),
        ("fix_bug.42", "fix-bug-42"),
        ("  Feat//Checkout__V2  ", "feat-checkout-v2"),
        ("--weird--", "weird"),
        ("UPPER", "upper"),
        ("a/b.c_d", "a-b-c-d"),
        ("émoji-✨-branch", "moji-branch"),
        ("123", "123"),
    ],
)
def test_slugify(branch: str, expected: str) -> None:
    assert slugify(branch) == expected


def test_slugify_is_deterministic() -> None:
    assert slugify("feature/abc") == slugify("feature/abc")


def test_truncation_adds_hash_and_avoids_collisions() -> None:
    a = slugify("feature/" + "x" * 60 + "-one")
    b = slugify("feature/" + "x" * 60 + "-two")
    assert len(a) <= 32 and len(b) <= 32
    assert a != b
    assert a.startswith("feature-xxx")


def test_truncation_does_not_end_on_dash() -> None:
    slug = slugify("a" * 24 + "/" + "b" * 30)
    assert not slug.endswith("-")
    assert "--" not in slug


@pytest.mark.parametrize("name", sorted(RESERVED))
def test_reserved_names_rejected(name: str) -> None:
    with pytest.raises(SlugError, match="reserved"):
        slugify(name)


def test_reserved_after_normalisation() -> None:
    with pytest.raises(SlugError):
        slugify("Traefik")
    with pytest.raises(SlugError):
        slugify("control_plane")


@pytest.mark.parametrize("branch", ["", "////", "___", "!!!", "x" * 256])
def test_unusable_branches(branch: str) -> None:
    with pytest.raises(SlugError):
        slugify(branch)


def test_collision_between_equivalent_branches() -> None:
    # Different branch names that normalise to the same slug collide on
    # purpose; the store's unique index then returns 409.
    assert slugify("feat/x") == slugify("feat_x") == slugify("FEAT.x")


@pytest.mark.parametrize(
    ("raw", "owner"), [("Alice.Smith", "alice-smith"), ("bob", "bob"), (" Ça va ", "a-va")]
)
def test_sanitize_owner(raw: str, owner: str) -> None:
    assert sanitize_owner(raw) == owner


def test_sanitize_owner_rejects_empty() -> None:
    with pytest.raises(SlugError):
        sanitize_owner("!!!")


@pytest.mark.parametrize(
    ("raw", "seconds"), [("30s", 30), ("5m", 300), ("24h", 86400), ("2d", 172800)]
)
def test_parse_duration(raw: str, seconds: int) -> None:
    assert parse_duration(raw) == seconds


@pytest.mark.parametrize("raw", ["", "24", "h", "1.5h", "-1h", "1w", "1 h", "0s", "24H", "1h2m"])
def test_parse_duration_rejects(raw: str) -> None:
    with pytest.raises(TTLError):
        parse_duration(raw)


def test_ttl_bounds() -> None:
    assert parse_ttl("60s", min_seconds=60, max_seconds=3600) == 60
    with pytest.raises(TTLError, match="below the minimum"):
        parse_ttl("59s", min_seconds=60, max_seconds=3600)
    with pytest.raises(TTLError, match="exceeds the maximum"):
        parse_ttl("2h", min_seconds=60, max_seconds=3600)


def test_owner_tokens_parsing() -> None:
    assert parse_owner_tokens("alice:a1, bob:b2") == {"alice": "a1", "bob": "b2"}
    assert parse_owner_tokens("") == {}
    for bad in ("alice", "alice:", ":tok", "a:1,a:2"):
        with pytest.raises(ConfigError):
            parse_owner_tokens(bad)


def _env(**kw: str) -> dict[str, str]:
    base = {
        "ADMIN_TOKEN": "a" * 32,
        "SLACK_SIGNING_SECRET": "s" * 32,
        "OWNER_TOKENS": "alice:" + "x" * 32,
    }
    base.update(kw)
    return base


def test_settings_from_env_defaults() -> None:
    s = Settings.from_env(_env())
    assert s.max_envs_per_owner == 3
    assert s.max_total_memory_mb == 1024
    assert s.env_memory_mb == 192
    assert s.max_total_lifetime_hours == 72
    assert s.env_url("demo") == "http://demo.localhost:8080"


def test_settings_from_env_overrides() -> None:
    s = Settings.from_env(_env(MIN_TTL_SECONDS="10", EDGE_PORT="80", COST_PER_GB_HOUR="0.5"))
    assert s.min_ttl_seconds == 10
    assert s.env_url("demo") == "http://demo.localhost"
    assert s.cost_per_gb_hour == 0.5


@pytest.mark.parametrize(
    "env",
    [
        {"ADMIN_TOKEN": ""},
        {"SLACK_SIGNING_SECRET": "short"},
        {"MAX_ENVS_PER_OWNER": "abc"},
        {"MAX_ENVS_PER_OWNER": "0"},
        {"COST_PER_GB_HOUR": "x"},
        {"COST_PER_GB_HOUR": "-1"},
        {"MAX_TTL_HOURS": "100", "MAX_TOTAL_LIFETIME_HOURS": "72"},
        {"MIN_TTL_SECONDS": "7200", "MAX_TTL_HOURS": "1"},
    ],
)
def test_settings_from_env_rejects(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(_env(**env))
