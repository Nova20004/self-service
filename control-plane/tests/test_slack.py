from __future__ import annotations

from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app.models import FakeClock
from app.slack import SignatureError, SlackVerifier, parse_command_text, sign

from .conftest import SECRET

PATH = "/v1/chatops/create-env"


def payload(text: str = "branch=feat-checkout ttl=24h", user: str = "Carol.D") -> bytes:
    return urlencode(
        {"command": "/create-env", "text": text, "user_name": user, "team_domain": "acme"}
    ).encode()


def post(client: TestClient, body: bytes, ts: str, sig: str):
    return client.post(
        PATH,
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    )


def test_valid_signature_creates_env(client: TestClient, clock: FakeClock) -> None:
    body = payload()
    ts = str(int(clock.now()))
    resp = post(client, body, ts, sign(SECRET, ts, body))
    assert resp.status_code == 200
    data = resp.json()
    assert data["response_type"] == "ephemeral"
    assert "feat-checkout" in data["text"] and "postgresql://" in data["text"]
    assert data["env"]["status"] == "PROVISIONING"
    assert data["env"]["url"] == "http://feat-checkout.localhost:8080"


def test_owner_is_sanitized_user_name(client: TestClient, clock: FakeClock, platform) -> None:
    body = payload(user="Carol.D")
    ts = str(int(clock.now()))
    post(client, body, ts, sign(SECRET, ts, body))
    assert platform.store.get_by_slug("feat-checkout").owner == "carol-d"


def test_tampered_body_rejected(client: TestClient, clock: FakeClock) -> None:
    ts = str(int(clock.now()))
    sig = sign(SECRET, ts, payload())
    resp = post(client, payload(text="branch=evil ttl=24h"), ts, sig)
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_wrong_secret_rejected(client: TestClient, clock: FakeClock) -> None:
    ts = str(int(clock.now()))
    body = payload()
    assert post(client, body, ts, sign("other-secret-xxxxxxxx", ts, body)).status_code == 401


def test_stale_timestamp_rejected(client: TestClient, clock: FakeClock) -> None:
    ts = str(int(clock.now()) - 301)
    body = payload()
    resp = post(client, body, ts, sign(SECRET, ts, body))
    assert resp.status_code == 401 and "stale" in resp.json()["detail"]


def test_replay_rejected(client: TestClient, clock: FakeClock) -> None:
    ts = str(int(clock.now()))
    body = payload(text="branch=once ttl=1h")
    sig = sign(SECRET, ts, body)
    assert post(client, body, ts, sig).status_code == 200
    resp = post(client, body, ts, sig)
    assert resp.status_code == 401 and "replay" in resp.json()["detail"]


def test_missing_headers_rejected(client: TestClient) -> None:
    assert client.post(PATH, content=payload()).status_code == 401


def test_domain_errors_are_ephemeral_messages(client: TestClient, clock: FakeClock) -> None:
    for text in ("branch=admin", "ttl=1h", "branch=ok ttl=bad", "a b"):
        body = payload(text=text)
        ts = str(int(clock.now()))
        resp = post(client, body, ts, sign(SECRET, ts, body))
        assert resp.status_code == 200, text
        assert resp.json()["response_type"] == "ephemeral"
        assert resp.json()["text"].startswith(":x:")
        clock.advance(1)


def test_verifier_unit() -> None:
    v = SlackVerifier(SECRET)
    body = b"a=1"
    v.verify(timestamp="1000", signature=sign(SECRET, "1000", body), body=body, now=1000)
    with pytest.raises(SignatureError, match="malformed"):
        v.verify(timestamp="abc", signature="v0=x", body=body, now=1000)
    with pytest.raises(SignatureError, match="mismatch"):
        v.verify(timestamp="1000", signature="v0=deadbeef", body=body, now=1000)
    # the seen-signature cache expires with the freshness window
    v.verify(timestamp="2000", signature=sign(SECRET, "2000", body), body=body, now=2000)
    assert len(v._seen) == 1


def test_parse_command_text() -> None:
    assert parse_command_text("branch=x ttl=2h") == {"branch": "x", "ttl": "2h"}
    assert parse_command_text("feat/y ttl=1h") == {"branch": "feat/y", "ttl": "1h"}
    with pytest.raises(ValueError):
        parse_command_text("feat extra")
