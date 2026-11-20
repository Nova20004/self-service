"""Container runtime abstraction.

``Runtime`` is the seam between the control plane and the container engine:
``DockerRuntime`` talks to the Docker Engine API through ``docker-proxy-rw``;
``FakeRuntime`` is an in-memory double used by the unit tests. A Kubernetes
backend would be another implementation of the same protocol.
"""

from __future__ import annotations

import json
import logging
import struct
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

import httpx

log = logging.getLogger(__name__)

MANAGED_BY_KEY = "managed-by"
MANAGED_BY_VALUE = "ephemeral-idp"
ENV_ID_LABEL = "ephemeral.env-id"
OWNER_LABEL = "ephemeral.owner"
SLUG_LABEL = "ephemeral.slug"
EXPIRES_LABEL = "ephemeral.expires-at"
ROLE_LABEL = "ephemeral.role"

ResourceKind = Literal["container", "network", "volume"]


class RuntimeError_(Exception):
    """A runtime call failed (named to avoid shadowing the builtin)."""


@dataclass(frozen=True)
class HealthCheck:
    test: list[str]
    interval_s: float = 5.0
    timeout_s: float = 3.0
    retries: int = 3
    start_period_s: float = 60.0
    # Probe often while starting (fast provisioning), rarely afterwards (cheap).
    start_interval_s: float = 1.0


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    image: str
    network: str
    labels: dict[str, str]
    env: dict[str, str] = field(default_factory=dict)
    command: list[str] | None = None
    user: str | None = None
    memory_mb: int = 64
    cpus: float = 0.25
    pids_limit: int = 64
    cap_add: list[str] = field(default_factory=list)
    read_only: bool = True
    tmpfs: dict[str, str] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    volumes: dict[str, str] = field(default_factory=dict)
    healthcheck: HealthCheck | None = None


@dataclass(frozen=True)
class ContainerState:
    status: str
    health: str | None
    exit_code: int | None = None


@dataclass(frozen=True)
class ManagedResource:
    kind: ResourceKind
    name: str
    env_id: str | None
    created_at: float


class Runtime(Protocol):
    def ping(self) -> bool: ...

    def image_exists(self, ref: str) -> bool: ...

    def create_network(self, name: str, labels: dict[str, str]) -> None: ...

    def remove_network(self, name: str) -> None: ...

    def connect(self, network: str, container: str) -> None: ...

    def disconnect(self, network: str, container: str) -> None: ...

    def create_volume(self, name: str, labels: dict[str, str]) -> None: ...

    def remove_volume(self, name: str) -> None: ...

    def run_container(self, spec: ContainerSpec) -> None: ...

    def remove_container(self, name: str) -> None: ...

    def container_state(self, name: str) -> ContainerState | None: ...

    def container_logs(self, name: str, tail: int = 20) -> str: ...

    def list_managed(self) -> list[ManagedResource]: ...


def _parse_rfc3339(raw: str) -> float:
    if not raw:
        return 0.0
    raw = raw.replace("Z", "+00:00")
    if "." in raw:
        head, _, rest = raw.partition(".")
        digits = len(rest) - len(rest.lstrip("0123456789"))
        frac, tz = rest[:digits], rest[digits:]
        raw = f"{head}.{frac[:6]}{tz}"
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return 0.0


def _managed_filter() -> str:
    return json.dumps({"label": [f"{MANAGED_BY_KEY}={MANAGED_BY_VALUE}"]})


class DockerRuntime:
    """Minimal Docker Engine API client restricted to what the provisioner needs.

    Every path used here must also be in the ``docker-proxy-rw`` allowlist in
    ``docker-compose.yml``; anything else gets a 403 from the proxy."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        tolerate: Iterable[int] = (),
    ) -> httpx.Response:
        try:
            resp = self._client.request(method, path, json=body, params=params)
        except httpx.HTTPError as exc:
            raise RuntimeError_(f"{method} {path}: {exc}") from exc
        if resp.status_code < 300 or resp.status_code in set(tolerate):
            return resp
        try:
            message = resp.json().get("message", resp.text)
        except ValueError:
            message = resp.text
        raise RuntimeError_(f"{method} {path} -> {resp.status_code}: {message.strip()[:300]}")

    def ping(self) -> bool:
        try:
            return self._client.get("/_ping").status_code == 200
        except httpx.HTTPError:
            return False

    def image_exists(self, ref: str) -> bool:
        return self._call("GET", f"/images/{ref}/json", tolerate=(404,)).status_code == 200

    def create_network(self, name: str, labels: dict[str, str]) -> None:
        self._call(
            "POST",
            "/networks/create",
            body={
                "Name": name,
                "Driver": "bridge",
                "Internal": True,
                "Attachable": False,
                "EnableIPv6": False,
                "Labels": labels,
            },
        )

    def remove_network(self, name: str) -> None:
        self._call("DELETE", f"/networks/{name}", tolerate=(404,))

    def connect(self, network: str, container: str) -> None:
        resp = self._call(
            "POST",
            f"/networks/{network}/connect",
            body={"Container": container},
            tolerate=(403, 409),
        )
        if resp.status_code in (403, 409) and "already" not in resp.text:
            raise RuntimeError_(f"connect {container} to {network}: {resp.text[:200]}")

    def disconnect(self, network: str, container: str) -> None:
        resp = self._call(
            "POST",
            f"/networks/{network}/disconnect",
            body={"Container": container, "Force": True},
            tolerate=(400, 403, 404, 409, 500),
        )
        if resp.status_code >= 300 and resp.status_code != 404 and "not connected" not in resp.text:
            raise RuntimeError_(f"disconnect {container} from {network}: {resp.text[:200]}")

    def create_volume(self, name: str, labels: dict[str, str]) -> None:
        self._call("POST", "/volumes/create", body={"Name": name, "Labels": labels})

    def remove_volume(self, name: str) -> None:
        self._call("DELETE", f"/volumes/{name}", tolerate=(404,))

    def run_container(self, spec: ContainerSpec) -> None:
        host_config: dict[str, Any] = {
            "NetworkMode": spec.network,
            "Memory": spec.memory_mb * 1024 * 1024,
            "MemorySwap": spec.memory_mb * 1024 * 1024,
            "NanoCpus": int(spec.cpus * 1_000_000_000),
            "PidsLimit": spec.pids_limit,
            "CapDrop": ["ALL"],
            "CapAdd": spec.cap_add or None,
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": spec.read_only,
            "Tmpfs": spec.tmpfs or None,
            "RestartPolicy": {"Name": "no"},
            "Privileged": False,
            "PublishAllPorts": False,
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "2"}},
            "Mounts": [
                {"Type": "volume", "Source": src, "Target": target}
                for src, target in spec.volumes.items()
            ],
        }
        body: dict[str, Any] = {
            "Image": spec.image,
            "Env": [f"{k}={v}" for k, v in spec.env.items()],
            "Labels": spec.labels,
            "HostConfig": host_config,
            "NetworkingConfig": {
                "EndpointsConfig": {spec.network: {"Aliases": spec.aliases or None}}
            },
        }
        if spec.command is not None:
            body["Cmd"] = spec.command
        if spec.user:
            body["User"] = spec.user
        if spec.healthcheck:
            hc = spec.healthcheck
            body["Healthcheck"] = {
                "Test": hc.test,
                "Interval": int(hc.interval_s * 1e9),
                "Timeout": int(hc.timeout_s * 1e9),
                "Retries": hc.retries,
                "StartPeriod": int(hc.start_period_s * 1e9),
                "StartInterval": int(hc.start_interval_s * 1e9),
            }
        self._call("POST", "/containers/create", body=body, params={"name": spec.name})
        self._call("POST", f"/containers/{spec.name}/start")

    def remove_container(self, name: str) -> None:
        self._call(
            "DELETE",
            f"/containers/{name}",
            params={"force": "true", "v": "false"},
            tolerate=(404,),
        )

    def container_state(self, name: str) -> ContainerState | None:
        resp = self._call("GET", f"/containers/{name}/json", tolerate=(404,))
        if resp.status_code == 404:
            return None
        state = resp.json().get("State", {})
        health = (state.get("Health") or {}).get("Status")
        return ContainerState(
            status=state.get("Status", "unknown"), health=health, exit_code=state.get("ExitCode")
        )

    def container_logs(self, name: str, tail: int = 20) -> str:
        resp = self._call(
            "GET",
            f"/containers/{name}/logs",
            params={"stdout": "1", "stderr": "1", "tail": str(tail)},
            tolerate=(404,),
        )
        if resp.status_code == 404:
            return ""
        return _demux(resp.content)

    def list_managed(self) -> list[ManagedResource]:
        flt = {"filters": _managed_filter()}
        out: list[ManagedResource] = []
        for c in self._call("GET", "/containers/json", params={"all": "1", **flt}).json():
            labels = c.get("Labels") or {}
            names = c.get("Names") or [c.get("Id", "")]
            out.append(
                ManagedResource(
                    "container",
                    names[0].lstrip("/"),
                    labels.get(ENV_ID_LABEL),
                    float(c.get("Created", 0)),
                )
            )
        for n in self._call("GET", "/networks", params=flt).json():
            labels = n.get("Labels") or {}
            out.append(
                ManagedResource(
                    "network",
                    n["Name"],
                    labels.get(ENV_ID_LABEL),
                    _parse_rfc3339(n.get("Created", "")),
                )
            )
        volumes = self._call("GET", "/volumes", params=flt).json().get("Volumes") or []
        for v in volumes:
            labels = v.get("Labels") or {}
            out.append(
                ManagedResource(
                    "volume",
                    v["Name"],
                    labels.get(ENV_ID_LABEL),
                    _parse_rfc3339(v.get("CreatedAt", "")),
                )
            )
        return out


def _demux(raw: bytes) -> str:
    """Decode Docker's multiplexed log stream (non-TTY containers)."""
    chunks: list[bytes] = []
    i = 0
    while i + 8 <= len(raw) and raw[i] in (0, 1, 2):
        (size,) = struct.unpack(">I", raw[i + 4 : i + 8])
        chunks.append(raw[i + 8 : i + 8 + size])
        i += 8 + size
    if i < len(raw):
        chunks.append(raw[i:])
    return b"".join(chunks).decode("utf-8", errors="replace")


@dataclass
class _FakeContainer:
    spec: ContainerSpec
    created_at: float
    status: str = "running"
    health: str | None = "healthy"


class FakeRuntime:
    """In-memory runtime with failure injection for unit tests.

    ``fail`` holds operation keys that raise, e.g. ``"create_network"``,
    ``"run_container:db"``, ``"connect"``, ``"remove_volume"``.
    ``health`` maps a role (``app``/``db``) to the health status reported."""

    def __init__(self, clock: Any = None) -> None:
        self.clock = clock
        self.containers: dict[str, _FakeContainer] = {}
        self.networks: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        self.images: set[str] = set()
        self.fail: set[str] = set()
        self.fail_counts: dict[str, int] = {}
        self.health: dict[str, str] = {}
        self.calls: list[str] = []
        self.up = True

    def _now(self) -> float:
        return float(self.clock.now()) if self.clock is not None else 0.0

    def _maybe_fail(self, *keys: str) -> None:
        for key in keys:
            if key in self.fail:
                remaining = self.fail_counts.get(key)
                if remaining is not None:
                    if remaining <= 0:
                        continue
                    self.fail_counts[key] = remaining - 1
                raise RuntimeError_(f"injected failure: {key}")

    def ping(self) -> bool:
        return self.up

    def image_exists(self, ref: str) -> bool:
        return not self.images or ref in self.images

    def create_network(self, name: str, labels: dict[str, str]) -> None:
        self.calls.append(f"create_network:{name}")
        self._maybe_fail("create_network")
        if name in self.networks:
            raise RuntimeError_(f"network {name} already exists")
        self.networks[name] = {"labels": dict(labels), "members": set(), "created": self._now()}

    def remove_network(self, name: str) -> None:
        self.calls.append(f"remove_network:{name}")
        self._maybe_fail("remove_network")
        net = self.networks.get(name)
        if net is None:
            return
        attached = [c for c, fc in self.containers.items() if fc.spec.network == name]
        if net["members"] or attached:
            raise RuntimeError_(f"network {name} has active endpoints")
        del self.networks[name]

    def connect(self, network: str, container: str) -> None:
        self.calls.append(f"connect:{network}")
        self._maybe_fail("connect")
        if network not in self.networks:
            raise RuntimeError_(f"network {network} not found")
        self.networks[network]["members"].add(container)

    def disconnect(self, network: str, container: str) -> None:
        self.calls.append(f"disconnect:{network}")
        self._maybe_fail("disconnect")
        if network in self.networks:
            self.networks[network]["members"].discard(container)

    def create_volume(self, name: str, labels: dict[str, str]) -> None:
        self.calls.append(f"create_volume:{name}")
        self._maybe_fail("create_volume")
        self.volumes[name] = {"labels": dict(labels), "created": self._now()}

    def remove_volume(self, name: str) -> None:
        self.calls.append(f"remove_volume:{name}")
        self._maybe_fail("remove_volume")
        for fc in self.containers.values():
            if name in fc.spec.volumes:
                raise RuntimeError_(f"volume {name} is in use")
        self.volumes.pop(name, None)

    def run_container(self, spec: ContainerSpec) -> None:
        role = spec.labels.get(ROLE_LABEL, "")
        self.calls.append(f"run_container:{role}")
        self._maybe_fail("run_container", f"run_container:{role}")
        if spec.name in self.containers:
            raise RuntimeError_(f"container {spec.name} already exists")
        if spec.network not in self.networks:
            raise RuntimeError_(f"network {spec.network} not found")
        self.containers[spec.name] = _FakeContainer(
            spec=spec, created_at=self._now(), health=self.health.get(role, "healthy")
        )

    def remove_container(self, name: str) -> None:
        fc = self.containers.get(name)
        role = fc.spec.labels.get(ROLE_LABEL, "") if fc else ""
        self.calls.append(f"remove_container:{role or name}")
        self._maybe_fail("remove_container", f"remove_container:{role}")
        self.containers.pop(name, None)

    def container_state(self, name: str) -> ContainerState | None:
        fc = self.containers.get(name)
        if fc is None:
            return None
        return ContainerState(status=fc.status, health=fc.health)

    def container_logs(self, name: str, tail: int = 20) -> str:
        return "fake logs" if name in self.containers else ""

    def list_managed(self) -> list[ManagedResource]:
        self._maybe_fail("list_managed")
        out: list[ManagedResource] = []
        for name, fc in self.containers.items():
            if fc.spec.labels.get(MANAGED_BY_KEY) == MANAGED_BY_VALUE:
                out.append(
                    ManagedResource(
                        "container", name, fc.spec.labels.get(ENV_ID_LABEL), fc.created_at
                    )
                )
        for name, net in self.networks.items():
            if net["labels"].get(MANAGED_BY_KEY) == MANAGED_BY_VALUE:
                out.append(
                    ManagedResource(
                        "network", name, net["labels"].get(ENV_ID_LABEL), net["created"]
                    )
                )
        for name, vol in self.volumes.items():
            if vol["labels"].get(MANAGED_BY_KEY) == MANAGED_BY_VALUE:
                out.append(
                    ManagedResource("volume", name, vol["labels"].get(ENV_ID_LABEL), vol["created"])
                )
        return out
