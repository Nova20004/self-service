#!/usr/bin/env bash
# Pre-flight checks with actionable messages. Exit 1 if anything blocks `make up`.
set -uo pipefail

fail=0
ok()   { printf '  [ok]   %s\n' "$1"; }
warn() { printf '  [warn] %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; fail=1; }

echo "ephemeral-idp doctor"

if command -v docker >/dev/null 2>&1; then
  ok "docker CLI: $(docker --version)"
else
  bad "docker CLI not found. Install Docker Desktop (macOS) or Docker Engine (Linux)."
fi

if docker info >/dev/null 2>&1; then
  ok "Docker engine reachable ($(docker version --format '{{.Server.Version}}' 2>/dev/null))"
else
  bad "Docker engine not reachable. Start Docker Desktop, or on Linux: sudo systemctl start docker (and add yourself to the docker group)."
fi

if docker compose version >/dev/null 2>&1; then
  ok "Compose v2: $(docker compose version --short 2>/dev/null)"
else
  bad "docker compose (v2 plugin) missing. Update Docker Desktop or install docker-compose-plugin."
fi

if command -v python3 >/dev/null 2>&1; then
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    ok "python3 $(python3 -c 'import platform; print(platform.python_version())') (envctl needs >= 3.9)"
  else
    bad "python3 is older than 3.9; envctl and the demo need >= 3.9."
  fi
else
  bad "python3 not found; needed for envctl and the demo script."
fi

if command -v curl >/dev/null 2>&1; then ok "curl present"; else bad "curl not found."; fi

port_busy() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  else
    (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
  fi
}
env_val() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -n1; }
api_port="${API_PORT:-$(env_val API_PORT)}"; api_port="${api_port:-8000}"
edge_port="${EDGE_PORT:-$(env_val EDGE_PORT)}"; edge_port="${edge_port:-8080}"
for port in "${api_port}" "${edge_port}"; do
  owner=$(docker ps --filter "publish=${port}" --format '{{.Names}}' 2>/dev/null | head -n1)
  if [ -n "${owner}" ] && [[ "${owner}" == eidp-* ]]; then
    ok "port ${port} already served by ${owner} (this stack)"
  elif port_busy "${port}"; then
    bad "port ${port} is in use. Free it or set API_PORT/EDGE_PORT in .env."
  else
    ok "port ${port} free"
  fi
done

if [ "$(uname -s)" = "Linux" ] && [ -S /var/run/docker.sock ]; then
  gid=$(stat -c '%g' /var/run/docker.sock)
  ok "Docker socket group id ${gid} (socket-proxy runs as 65534:${gid})"
  if [ -f .env ] && ! grep -q "^DOCKER_SOCKET_GID=${gid}$" .env; then
    warn "DOCKER_SOCKET_GID in .env differs from ${gid}; edit .env or regenerate it."
  fi
else
  ok "Docker Desktop: socket-proxy runs as 65534:0 (socket is root-group inside the VM)"
fi

if [ -f .env ]; then ok ".env present"; else warn ".env missing - run: gmake env"; fi

# Compose gives exported shell variables precedence over .env.
for var in ADMIN_TOKEN OWNER_TOKENS SLACK_SIGNING_SECRET; do
  if [ -n "${!var:-}" ]; then
    warn "${var} is exported in this shell and overrides .env for docker compose; run: unset ${var}"
  fi
done

if [ "${fail}" -ne 0 ]; then
  echo "doctor: blocking problems found (see [FAIL] lines)."
  exit 1
fi
echo "doctor: all good."
