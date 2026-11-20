"""TTL grammar ``^\\d+[smhd]$`` and bounds."""

from __future__ import annotations

import re

TTL_RE = re.compile(r"^(\d{1,7})([smhd])$")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class TTLError(ValueError):
    pass


def parse_duration(raw: str) -> int:
    match = TTL_RE.match(raw.strip()) if isinstance(raw, str) else None
    if not match:
        raise TTLError(f"invalid duration {raw!r}: expected <number><s|m|h|d>, e.g. 24h")
    seconds = int(match.group(1)) * UNITS[match.group(2)]
    if seconds <= 0:
        raise TTLError("duration must be positive")
    return seconds


def parse_ttl(raw: str, *, min_seconds: int, max_seconds: int) -> int:
    seconds = parse_duration(raw)
    if seconds < min_seconds:
        raise TTLError(f"ttl {raw} is below the minimum of {min_seconds}s")
    if seconds > max_seconds:
        raise TTLError(f"ttl {raw} exceeds the maximum of {max_seconds // 3600}h")
    return seconds
