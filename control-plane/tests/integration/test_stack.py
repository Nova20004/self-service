"""Integration tests against the live stack (`make up`). Run in the e2e job:

    cd control-plane && pytest -m integration --no-cov

They read tokens from ../.env and need the docker CLI on the same host.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]


def _dotenv() -> dict[str, str]:
    path = ROOT / ".env"
    if not path.exists():
        pytest.skip(".env missing; run `make env && make up` first")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.startswith("#"):
            values[key.strip()] = value.strip()
    return values


ENV = _dotenv() if (ROOT / ".env").exists() else {}
API = f"http://127.0.0.1:{ENV.get('API_PORT', '8000')}"
EDGE = f"http://127.0.0.1:{ENV.get('EDGE_PORT', '8080')}"


def token(owner: str) -> str:
    for item in ENV.get("OWNER_TOKENS", "").split(","):
        name, _, tok = item.partition(":")
        if name == owner:
            return tok
    raise KeyError(owner)


def call(method: str, path: str, tok: str, body: dict[str, Any] | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def route(slug: str) -> tuple[int, str]:
    req = urllib.request.Request(EDGE + "/", headers={"Host": f"{slug}.localhost"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (TimeoutError, urllib.error.URLError):
        return 0, ""


def wait_route(slug: str, wanted: int, timeout: float = 30) -> int:
    deadline = time.monotonic() + timeout
    code = 0
    while time.monotonic() < deadline:
        code = route(slug)[0]
        if code == wanted:
            break
        time.sleep(1)
    return code


def wait_status(slug: str, tok: str, wanted: str, timeout: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _, body = call("GET", f"/v1/envs/{slug}", tok)
        if body.get("status") == wanted:
            return body
        time.sleep(1)
    raise AssertionError(f"{slug} never reached {wanted}: {body}")


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def test_ready() -> None:
    with urllib.request.urlopen(API + "/readyz", timeout=5) as resp:
        assert json.loads(resp.read()) == {
            "ready": True,
            "checks": {"database": True, "docker": True},
        }


def test_full_lifecycle_and_hardening() -> None:
    alice = token("alice")
    status, created = call("POST", "/v1/envs", alice, {"branch": "it/lifecycle", "ttl": "15m"})
    assert status == 202, created
    slug, env_id = created["slug"], created["id"]
    try:
        wait_status(slug, alice, "RUNNING")
        assert wait_route(slug, 200) == 200
        assert "DB: connected" in route(slug)[1]

        app = json.loads(docker("inspect", f"eidp-{env_id}-app").stdout)[0]
        db = json.loads(docker("inspect", f"eidp-{env_id}-db").stdout)[0]
        for c in (app, db):
            hc = c["HostConfig"]
            assert hc["CapDrop"] == ["ALL"] and not hc["CapAdd"]
            assert hc["Privileged"] is False and hc["ReadonlyRootfs"] is True
            assert hc["Memory"] == hc["MemorySwap"] and hc["Memory"] > 0
            assert hc["PidsLimit"] > 0 and hc["RestartPolicy"]["Name"] == "no"
            assert "no-new-privileges:true" in hc["SecurityOpt"]
            assert not hc.get("PortBindings") and not hc.get("Binds")
            assert c["Config"]["User"] not in ("", "0", "root")
            assert c["Config"]["Labels"]["managed-by"] == "ephemeral-idp"
        net = json.loads(docker("network", "inspect", f"eidp-env-{env_id}").stdout)[0]
        assert net["Internal"] is True
        members = {v["Name"] for v in net["Containers"].values()}
        assert members == {f"eidp-{env_id}-app", f"eidp-{env_id}-db", "eidp-traefik"}
    finally:
        call("DELETE", f"/v1/envs/{slug}", alice)
    wait_status(slug, alice, "DROPPED")
    assert wait_route(slug, 404) == 404
    assert docker("network", "inspect", f"eidp-env-{env_id}").returncode != 0


PROBE = (
    "import json,sys,urllib.request as u\n"
    "m,p,b=sys.argv[1],sys.argv[2],sys.argv[3]\n"
    "r=u.Request('http://'+sys.argv[4]+':2375'+p,data=(b.encode() if b else None),method=m,"
    "headers={'Content-Type':'application/json'})\n"
    "try:\n print(u.urlopen(r,timeout=5).status)\n"
    "except u.HTTPError as e:\n print(e.code)\n"
)


def probe(method: str, path: str, body: str = "", proxy: str = "docker-proxy-rw") -> str:
    out = docker("exec", "eidp-control-plane", "python", "-c", PROBE, method, path, body, proxy)
    return out.stdout.strip() or out.stderr.strip()


def test_rw_proxy_denies_what_the_provisioner_does_not_need() -> None:
    assert probe("GET", "/_ping") == "200"
    assert probe("GET", "/info") == "403"
    assert probe("POST", "/images/create?fromImage=alpine") == "403"
    assert probe("POST", "/containers/x/exec", "{}") == "403"
    assert probe("POST", "/build") == "403"
    bind = json.dumps(
        {"Image": "ephemeral-idp/workload:0.1.0", "HostConfig": {"Binds": ["/etc:/host-etc:ro"]}}
    )
    assert probe("POST", "/containers/create?name=eidp-it-bind", bind) == "403"
    assert docker("inspect", "eidp-it-bind").returncode != 0


def test_ro_proxy_rejects_the_control_plane() -> None:
    # docker-proxy-ro only accepts Traefik, and the control plane is not even
    # on its network.
    assert probe("GET", "/_ping", proxy="docker-proxy-ro") not in ("200", "403")
