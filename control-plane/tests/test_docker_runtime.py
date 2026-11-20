from __future__ import annotations

import json
import struct

import httpx
import pytest

from app.runtime import (
    ContainerSpec,
    DockerRuntime,
    HealthCheck,
    RuntimeError_,
    _demux,
    _parse_rfc3339,
)


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key in self.routes:
            return self.routes[key]
        return httpx.Response(204)


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def rt(rec: Recorder) -> DockerRuntime:
    return DockerRuntime("http://proxy:2375", transport=httpx.MockTransport(rec))


def test_run_container_payload(rt: DockerRuntime, rec: Recorder) -> None:
    rec.routes[("POST", "/containers/create")] = httpx.Response(201, json={"Id": "abc"})
    spec = ContainerSpec(
        name="eidp-1-db",
        image="postgres:17",
        network="eidp-env-1",
        labels={"managed-by": "ephemeral-idp"},
        env={"A": "1"},
        command=["postgres"],
        user="70:70",
        memory_mb=128,
        cpus=0.5,
        pids_limit=100,
        tmpfs={"/tmp": "size=1m"},
        aliases=["db"],
        volumes={"vol": "/data"},
        healthcheck=HealthCheck(test=["CMD", "true"], interval_s=1),
    )
    rt.run_container(spec)
    create, start = rec.requests
    assert create.url.params["name"] == "eidp-1-db"
    body = json.loads(create.content)
    hc = body["HostConfig"]
    assert hc["Memory"] == hc["MemorySwap"] == 128 * 1024 * 1024
    assert hc["CapDrop"] == ["ALL"] and hc["CapAdd"] is None
    assert hc["SecurityOpt"] == ["no-new-privileges:true"]
    assert hc["ReadonlyRootfs"] is True and hc["Privileged"] is False
    assert hc["RestartPolicy"] == {"Name": "no"}
    assert hc["NanoCpus"] == 500_000_000 and hc["PidsLimit"] == 100
    assert hc["Mounts"] == [{"Type": "volume", "Source": "vol", "Target": "/data"}]
    assert "Binds" not in hc and "PortBindings" not in hc
    assert body["User"] == "70:70" and body["Cmd"] == ["postgres"]
    assert body["Healthcheck"]["Interval"] == 1_000_000_000
    assert body["Healthcheck"]["StartInterval"] == 1_000_000_000
    assert body["Healthcheck"]["StartPeriod"] == 60_000_000_000
    assert body["NetworkingConfig"]["EndpointsConfig"]["eidp-env-1"]["Aliases"] == ["db"]
    assert start.url.path == "/containers/eidp-1-db/start"


def test_network_volume_calls(rt: DockerRuntime, rec: Recorder) -> None:
    rt.create_network("n1", {"managed-by": "ephemeral-idp"})
    assert json.loads(rec.requests[-1].content)["Internal"] is True
    rt.create_volume("v1", {})
    rt.connect("n1", "eidp-traefik")
    rt.disconnect("n1", "eidp-traefik")
    assert json.loads(rec.requests[-1].content) == {"Container": "eidp-traefik", "Force": True}
    rt.remove_network("n1")
    rt.remove_volume("v1")
    rt.remove_container("c1")
    assert rec.requests[-1].url.params["force"] == "true"


def test_404_tolerated_on_removal(rt: DockerRuntime, rec: Recorder) -> None:
    for path in ("/networks/n", "/volumes/v", "/containers/c"):
        rec.routes[("DELETE", path)] = httpx.Response(404, json={"message": "no such"})
    rt.remove_network("n")
    rt.remove_volume("v")
    rt.remove_container("c")


def test_errors_raise(rt: DockerRuntime, rec: Recorder) -> None:
    rec.routes[("DELETE", "/volumes/v")] = httpx.Response(409, json={"message": "in use"})
    with pytest.raises(RuntimeError_, match="409: in use"):
        rt.remove_volume("v")
    rec.routes[("POST", "/networks/create")] = httpx.Response(403, text="blocked by proxy")
    with pytest.raises(RuntimeError_, match="403: blocked"):
        rt.create_network("x", {})


def test_connect_already_connected_ok(rt: DockerRuntime, rec: Recorder) -> None:
    rec.routes[("POST", "/networks/n/connect")] = httpx.Response(
        403, json={"message": "endpoint already exists"}
    )
    rt.connect("n", "t")
    rec.routes[("POST", "/networks/n/connect")] = httpx.Response(403, json={"message": "denied"})
    with pytest.raises(RuntimeError_):
        rt.connect("n", "t")


def test_disconnect_tolerances(rt: DockerRuntime, rec: Recorder) -> None:
    for status, msg in ((404, "no such network"), (403, "container is not connected")):
        rec.routes[("POST", "/networks/n/disconnect")] = httpx.Response(
            status, json={"message": msg}
        )
        rt.disconnect("n", "t")
    rec.routes[("POST", "/networks/n/disconnect")] = httpx.Response(500, json={"message": "boom"})
    with pytest.raises(RuntimeError_):
        rt.disconnect("n", "t")


def test_state_logs_images_ping(rt: DockerRuntime, rec: Recorder) -> None:
    rec.routes[("GET", "/containers/c/json")] = httpx.Response(
        200, json={"State": {"Status": "running", "Health": {"Status": "healthy"}}}
    )
    state = rt.container_state("c")
    assert state is not None and state.health == "healthy"
    rec.routes[("GET", "/containers/c/json")] = httpx.Response(404, json={})
    assert rt.container_state("c") is None
    frame = b"\x01\x00\x00\x00" + struct.pack(">I", 5) + b"hello"
    rec.routes[("GET", "/containers/c/logs")] = httpx.Response(200, content=frame)
    assert rt.container_logs("c") == "hello"
    rec.routes[("GET", "/containers/c/logs")] = httpx.Response(404)
    assert rt.container_logs("c") == ""
    rec.routes[("GET", "/images/postgres:17/json")] = httpx.Response(200, json={})
    assert rt.image_exists("postgres:17")
    rec.routes[("GET", "/_ping")] = httpx.Response(200, text="OK")
    assert rt.ping()


def test_ping_handles_transport_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    rt = DockerRuntime("http://proxy:2375", transport=httpx.MockTransport(boom))
    assert rt.ping() is False
    with pytest.raises(RuntimeError_, match="refused"):
        rt.remove_volume("v")
    rt.close()


def test_list_managed(rt: DockerRuntime, rec: Recorder) -> None:
    rec.routes[("GET", "/containers/json")] = httpx.Response(
        200, json=[{"Names": ["/eidp-1-app"], "Labels": {"ephemeral.env-id": "1"}, "Created": 100}]
    )
    rec.routes[("GET", "/networks")] = httpx.Response(
        200,
        json=[
            {
                "Name": "eidp-env-1",
                "Labels": {"ephemeral.env-id": "1"},
                "Created": "2026-11-20T06:27:56.123456789Z",
            }
        ],
    )
    rec.routes[("GET", "/volumes")] = httpx.Response(
        200,
        json={
            "Volumes": [
                {"Name": "eidp-1-pgdata", "Labels": None, "CreatedAt": "2026-11-20T06:27:56Z"}
            ]
        },
    )
    res = rt.list_managed()
    assert [(r.kind, r.name, r.env_id) for r in res] == [
        ("container", "eidp-1-app", "1"),
        ("network", "eidp-env-1", "1"),
        ("volume", "eidp-1-pgdata", None),
    ]
    assert json.loads(rec.requests[0].url.params["filters"]) == {
        "label": ["managed-by=ephemeral-idp"]
    }
    assert res[1].created_at == pytest.approx(res[2].created_at + 0.123456)


def test_helpers() -> None:
    assert _parse_rfc3339("") == 0.0
    assert _parse_rfc3339("garbage") == 0.0
    assert _demux(b"plain text") == "plain text"
