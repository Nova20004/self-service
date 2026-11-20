# ADR 0003 - Traefik with host-based routing on `*.localhost`

- Status: accepted
- Date: 2026-11-20

## Context

Each environment needs a stable, human-readable URL without publishing a host
port per environment and without DNS or TLS setup.

## Decision

- Traefik v3 with the Docker provider (through `docker-proxy-ro`),
  `exposedByDefault=false` and a constraint
  ``Label(`managed-by`, `ephemeral-idp`)``: only containers created by the
  platform with `traefik.enable=true` are routed.
- Router rule ``Host(`<slug>.localhost`)`` on one entrypoint published on
  `127.0.0.1:${EDGE_PORT}`; `traefik.docker.network` selects the env network.
- Each environment network is `internal`; the provisioner connects the Traefik
  container to it as a routing step and disconnects it on teardown.
- Static configuration is baked into a derived image (no bind mount); the
  dashboard and API are disabled; ping is bound to 127.0.0.1 inside the container.

## Consequences

- Browsers resolve `*.localhost` to loopback (RFC 6761); for CLI tools the
  portable form is `curl -H 'Host: <slug>.localhost' http://127.0.0.1:8080/`.
- No TLS (roadmap: local CA / ACME).
- Traefik is attached to every routed environment network, so it is the one
  component that can reach all apps; it has no Docker write access.
