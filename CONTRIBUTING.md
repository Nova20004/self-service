# Contributing

Thanks for helping. Small, focused pull requests with tests are the easiest to review.

## Setup

```bash
gmake doctor && gmake env
gmake test                 # creates .venv (Python >= 3.13), runs unit tests + coverage gate
gmake validate             # compose config, yamllint, ruff, mypy --strict, hadolint, shellcheck
gmake demo                 # full lifecycle against real Docker
```

`hadolint` and `shellcheck` are optional locally (reported as SKIPPED) but
required in CI.

## Rules of the codebase

- Control plane: Python 3.13, `mypy --strict`, `ruff` (config in
  `control-plane/pyproject.toml`), coverage >= 85 %.
- `cli/envctl`: standard library only, must run on Python 3.9.
- Makefile: must keep working with GNU Make 3.81 (no `.ONESHELL`, `$(file …)`,
  grouped targets).
- Containers: no `privileged`, no host network/PID, no host bind mounts; the
  Docker socket is mounted read-only into the socket proxies only. New Docker
  API calls need a matching, minimal allow-list entry in `docker-compose.yml`.
- Pin everything: image tags with digests, GitHub Actions by full commit SHA,
  Python packages by exact version, and record changes in `docs/VERSIONS.md`
  (releases younger than 14 days are not adopted).
- No secrets in the repository; configuration goes through `.env`.
- Significant design changes come with an ADR in `docs/adr/`.

## Tests

- Unit tests use `FakeRuntime` and `FakeClock`; no Docker needed.
- Integration tests (`pytest -m integration`) run against a live stack and are
  executed by the CI `e2e` job.
- Bug fixes start with a failing test.

## Commit messages

Imperative mood, one logical change per commit (e.g. `reconciler: cap teardown backoff`).
