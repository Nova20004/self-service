"""Tiny per-environment workload: shows its identity and proves DB connectivity.

``EXPIRES_AT`` is injected once at creation. Extending an environment updates
the control plane (API/CLI), not this value.
"""

from __future__ import annotations

import html
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

IDENTITY = {
    "branch": os.environ.get("BRANCH", "unknown"),
    "env_id": os.environ.get("ENV_ID", "unknown"),
    "slug": os.environ.get("ENV_SLUG", "unknown"),
    "owner": os.environ.get("OWNER", "unknown"),
    "expires_at_at_creation": os.environ.get("EXPIRES_AT", "unknown"),
}
DATABASE_URL = os.environ.get("DATABASE_URL", "")


def db_version() -> tuple[bool, str]:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            row = conn.execute("SELECT version()").fetchone()
            return True, str(row[0]) if row else "unknown"
    except Exception as exc:
        return False, type(exc).__name__


class Handler(BaseHTTPRequestHandler):
    server_version = "ephemeral-workload/0.1"
    sys_version = ""

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            ok, _ = db_version()
            status = 200 if ok else 503
            self._send(status, json.dumps({"ok": ok}).encode(), "application/json")
            return
        ok, version = db_version()
        if self.path == "/api":
            payload = {**IDENTITY, "db_connected": ok, "db_version": version}
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        rows = "".join(
            f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>"
            for k, v in IDENTITY.items()
        )
        db = f"connected - {html.escape(version)}" if ok else f"NOT connected ({version})"
        page = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(IDENTITY['slug'])}</title></head><body>"
            f"<h1>Ephemeral environment {html.escape(IDENTITY['slug'])}</h1>"
            f"<table>{rows}<tr><th>database</th><td>DB: {db}</td></tr></table>"
            "<p><small>expires_at is the value injected at creation; extensions are "
            "visible through the API and envctl.</small></p></body></html>"
        )
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def log_message(self, fmt: str, *args: object) -> None:
        if self.path != "/healthz":
            sys.stdout.write(json.dumps({"msg": fmt % args, "env_id": IDENTITY["env_id"]}) + "\n")


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - container port
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
