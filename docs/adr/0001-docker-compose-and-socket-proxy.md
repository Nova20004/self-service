# ADR 0001 - Docker Compose runtime behind a socket proxy

- Status: accepted
- Date: 2026-11-20

## Context

The platform must run on a laptop with no cloud account and be started with one
command, while creating containers on demand. Creating containers requires the
Docker API, and raw access to `/var/run/docker.sock` is root-equivalent on the host.

## Decision

- Run the platform with Docker Compose; environments are plain containers,
  networks and volumes created through the Docker Engine API.
- Never mount the socket into the control plane or Traefik. Mount it read-only
  into two `wollomatic/socket-proxy` containers only:
  - `docker-proxy-rw` for the control plane: method + path allow-list of the
    exact calls the provisioner makes, `-allowfrom=control-plane`, and
    `-allowbindmountfrom` pointing at a non-existent directory to reject every
    bind mount;
  - `docker-proxy-ro` for Traefik: GET/HEAD only, `-allowfrom=traefik`.
- Each proxy sits on its own `internal` network with its single client.
- Hide the runtime behind a `Runtime` protocol so a Kubernetes runtime can be
  added later and tests run against `FakeRuntime`.

## Consequences

- One `gmake demo` brings everything up; no cluster to install.
- A compromise of Traefik or of the control plane does not give unrestricted
  Docker access; exec, build, pull and host bind mounts are refused (tested).
- The proxy cannot validate every field of the request body (see
  THREAT-MODEL); the control plane builds container specs in code only.
- Socket permissions differ between Docker Desktop (gid 0) and Linux (`docker`
  group): handled by `DOCKER_SOCKET_GID`, detected by `gmake env`.
- Single host only; Kubernetes is on the roadmap.
