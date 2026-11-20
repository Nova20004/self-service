"""RFC 9457 problem details."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"

_TITLES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    422: "Unprocessable Content",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        detail: str,
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code or _TITLES.get(status, "error").lower().replace(" ", "-")
        self.headers = headers or {}
        self.extra = extra or {}


def problem(
    status: int,
    detail: str,
    *,
    instance: str,
    code: str | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"urn:ephemeral-idp:problem:{code}" if code else "about:blank",
        "title": _TITLES.get(status, "Error"),
        "status": status,
        "detail": detail,
        "instance": instance,
    }
    if code:
        body["code"] = code
    body.update(extra or {})
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiError):  # pragma: no cover - registered for ApiError only
        raise exc
    return problem(
        exc.status,
        exc.detail,
        instance=request.url.path,
        code=exc.code,
        headers=exc.headers,
        extra=exc.extra,
    )


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        raise exc
    return problem(
        exc.status_code,
        str(exc.detail),
        instance=request.url.path,
        headers=dict(exc.headers or {}),
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    errors = [
        {"loc": [str(p) for p in err.get("loc", ())], "msg": str(err.get("msg", ""))}
        for err in exc.errors()
    ]
    return problem(
        400,
        "request validation failed",
        instance=request.url.path,
        code="validation-error",
        extra={"errors": errors},
    )
