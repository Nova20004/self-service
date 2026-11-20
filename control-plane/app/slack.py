"""Slack slash-command support: request signature verification and parsing.

Signature scheme (Slack "Verifying requests"): ``v0=`` + hex
HMAC-SHA256(signing_secret, ``v0:{timestamp}:{raw body}``).
"""

from __future__ import annotations

import hashlib
import hmac
import shlex
import threading
from urllib.parse import parse_qs

MAX_SKEW_SECONDS = 300


class SignatureError(Exception):
    pass


def sign(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


class SlackVerifier:
    """Verifies signatures and rejects replays inside the freshness window."""

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(
        self, *, timestamp: str | None, signature: str | None, body: bytes, now: float
    ) -> None:
        if not timestamp or not signature:
            raise SignatureError("missing Slack signature headers")
        try:
            ts = int(timestamp)
        except ValueError as exc:
            raise SignatureError("malformed timestamp") from exc
        if abs(now - ts) > MAX_SKEW_SECONDS:
            raise SignatureError("stale request timestamp")
        expected = sign(self._secret, timestamp, body)
        if not hmac.compare_digest(expected.encode(), signature.encode()):
            raise SignatureError("signature mismatch")
        with self._lock:
            for key, seen_at in list(self._seen.items()):
                if now - seen_at > MAX_SKEW_SECONDS:
                    del self._seen[key]
            if signature in self._seen:
                raise SignatureError("replayed request")
            self._seen[signature] = now


def parse_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8", errors="strict"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def parse_command_text(text: str) -> dict[str, str]:
    """``branch=feat-checkout ttl=24h`` -> dict. A bare first word is the branch."""
    args: dict[str, str] = {}
    for i, token in enumerate(shlex.split(text)):
        key, sep, value = token.partition("=")
        if sep:
            args[key.strip().lower()] = value.strip()
        elif i == 0:
            args["branch"] = token
        else:
            raise ValueError(f"unexpected argument {token!r}; use key=value")
    return args


def ephemeral(text: str, blocks: list[dict[str, object]] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"response_type": "ephemeral", "text": text}
    if blocks:
        payload["blocks"] = blocks
    return payload
