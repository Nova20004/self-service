"""Branch name -> deterministic DNS-safe slug."""

from __future__ import annotations

import hashlib
import re

MAX_SLUG_LEN = 32
HASH_LEN = 6
DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
RESERVED = frozenset(
    {
        "traefik",
        "api",
        "admin",
        "www",
        "localhost",
        "control-plane",
        "dashboard",
        "metrics",
        "healthz",
        "readyz",
        "docker",
        "docker-proxy",
        "proxy",
        "edge",
        "mail",
        "root",
    }
)


class SlugError(ValueError):
    pass


def slugify(branch: str) -> str:
    """Lowercase, map ``/ _ .`` to ``-``, drop other characters, collapse
    dashes, trim, and cap at 32 chars with a short hash suffix so two long
    branches sharing a prefix still get distinct slugs."""
    if not branch or len(branch) > 255:
        raise SlugError("branch must be 1-255 characters")
    lowered = branch.strip().lower()
    mapped = re.sub(r"[/_.]", "-", lowered)
    cleaned = re.sub(r"[^a-z0-9-]", "", mapped)
    collapsed = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not collapsed:
        raise SlugError(f"branch {branch!r} has no usable characters")
    if len(collapsed) > MAX_SLUG_LEN:
        digest = hashlib.sha256(branch.encode()).hexdigest()[:HASH_LEN]
        head = collapsed[: MAX_SLUG_LEN - HASH_LEN - 1].rstrip("-")
        collapsed = f"{head}-{digest}"
    if not DNS_LABEL.match(collapsed):
        raise SlugError(f"slug {collapsed!r} is not a valid DNS label")
    if collapsed in RESERVED:
        raise SlugError(f"slug {collapsed!r} is reserved")
    return collapsed


def sanitize_owner(name: str) -> str:
    """Owner names (e.g. Slack ``user_name``) reduced to a safe label."""
    cleaned = re.sub(r"[^a-z0-9-]", "-", name.strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")[:32].strip("-")
    if not cleaned:
        raise SlugError("owner name has no usable characters")
    return cleaned
