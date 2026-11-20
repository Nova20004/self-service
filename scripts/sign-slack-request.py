#!/usr/bin/env python3
"""Build a signed Slack slash-command request for /v1/chatops/create-env.

Prints a ready-to-run curl command, or with --send performs the request and
prints the JSON response. Use --tamper to corrupt the signature (demo of the
401 path). Reads SLACK_SIGNING_SECRET from the environment or --env-file.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def read_secret(env_file: str | None) -> str:
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret and env_file:
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.strip().partition("=")
                if key == "SLACK_SIGNING_SECRET":
                    secret = value.strip().strip("'\"")
    if not secret:
        sys.exit("SLACK_SIGNING_SECRET is not set (export it or pass --env-file .env)")
    return secret


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000/v1/chatops/create-env")
    parser.add_argument("--text", default="branch=feat-checkout ttl=24h")
    parser.add_argument("--user", default="carol")
    parser.add_argument("--env-file")
    parser.add_argument("--tamper", action="store_true", help="send an invalid signature")
    parser.add_argument("--send", action="store_true", help="perform the request")
    args = parser.parse_args()

    secret = read_secret(args.env_file)
    body = urllib.parse.urlencode(
        {"command": "/create-env", "text": args.text, "user_name": args.user,
         "team_domain": "local", "channel_name": "dev"}
    ).encode()
    ts = str(int(time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if args.tamper:
        signature = signature[:-4] + ("0000" if not signature.endswith("0000") else "1111")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": signature,
    }
    if not args.send:
        parts = ["curl", "-sS", "-X", "POST", args.url]
        for k, v in headers.items():
            parts += ["-H", f"{k}: {v}"]
        parts += ["--data-raw", body.decode()]
        print(" ".join(shlex.quote(p) for p in parts))
        return 0

    req = urllib.request.Request(args.url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            print(f"HTTP {resp.status}")
            print(resp.read().decode())
            return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}")
        print(exc.read().decode())
        return 1


if __name__ == "__main__":
    sys.exit(main())
