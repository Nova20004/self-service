"""Bearer-token authentication. Only SHA-256 digests of tokens are kept in
memory and compared with ``hmac.compare_digest``."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from .api_errors import ApiError
from .models import Environment

ADMIN = "admin"


@dataclass(frozen=True)
class Principal:
    name: str
    is_admin: bool = False

    def can_access(self, env: Environment) -> bool:
        return self.is_admin or env.owner == self.name


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


class TokenAuthenticator:
    def __init__(self, admin_token: str, owner_tokens: dict[str, str]) -> None:
        self._entries: list[tuple[bytes, Principal]] = [
            (_digest(admin_token), Principal(ADMIN, is_admin=True))
        ]
        for owner, token in owner_tokens.items():
            if owner == ADMIN:
                raise ValueError("'admin' is reserved and cannot be an owner name")
            self._entries.append((_digest(token), Principal(owner)))

    def authenticate(self, authorization: str | None) -> Principal:
        if not authorization:
            raise ApiError(
                401,
                "missing bearer token",
                code="unauthenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ApiError(
                401,
                "expected 'Authorization: Bearer <token>'",
                code="unauthenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        presented = _digest(token.strip())
        match: Principal | None = None
        # Compare against every entry so timing does not reveal which one matched.
        for digest, principal in self._entries:
            if hmac.compare_digest(digest, presented):
                match = principal
        if match is None:
            raise ApiError(
                401, "invalid token", code="unauthenticated", headers={"WWW-Authenticate": "Bearer"}
            )
        return match
