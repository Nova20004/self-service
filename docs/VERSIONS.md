# Versions

Resolved on **2026-11-20**. Rule: use the latest stable release that is at least
14 days old (published on or before **2026-11-06**); newer releases are listed
under "Excluded". Dates come from the GitHub releases API, the Docker Hub tags
API and PyPI upload times, queried on the resolution date.

## Container images

| Image | Pinned reference | Upstream release | Notes |
|---|---|---|---|
| Python base (control plane, workload) | `python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90` | CPython v3.13.18 tag 2026-10-25 | Debian 13; `zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1` installed for a fixed HIGH; pip removed |
| Traefik | `traefik:v3.7.17@sha256:f4bb143d1135a579b630ed491e17ec50f6f7c9c6a30585ff3d385471d2a86f5a` | GitHub release 2026-10-30 | config baked into `ephemeral-idp/traefik:0.1.0` |
| Postgres | `postgres:17.11-alpine@sha256:e5d2edd28b1f8d40a0a6c0c9cdc545b19b56a0d5d88f94bc11a97d970d4700d0` | `REL_17_11` 2026-08-10 | derived `ephemeral-idp/postgres:17.11-alpine-p1`: `krb5-libs=1.22.3-r0`, `gosu` removed |
| Socket proxy | `wollomatic/socket-proxy:1.13.2@sha256:ff95d6ff6c27853005ef85eb8f4ab5923dc3d2f1e055f98fb6aeeaceec3fe848` | GitHub release 2026-10-24 | used unmodified for both proxies |

Docker Hub's "last pushed" date of a tag reflects image rebuilds (for example
`postgres:17.11-alpine` was re-pushed 2026-11-11); the 14-day rule is applied
to the upstream software release, and the digest pins the exact build we scanned.

Locally built images: `ephemeral-idp/control-plane:0.1.0`,
`ephemeral-idp/workload:0.1.0`, `ephemeral-idp/traefik:0.1.0`,
`ephemeral-idp/postgres:17.11-alpine-p1`.

## GitHub Actions

| Action | Tag | Commit SHA | Released |
|---|---|---|---|
| actions/checkout | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` | 2026-07-20 |
| actions/setup-python | v7.1.0 | `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0` | 2026-10-22 |
| actions/upload-artifact | v7.0.2 | `cf430e030ddbb5b0abf93d22962f4752f3646cd9` | 2026-10-07 |
| aquasecurity/trivy-action | v0.36.0 | `ed142fd0673e97e23eac54620cfb913e5ce36c25` | 2026-04-22 |
| hadolint/hadolint-action | v3.5.0 | `06be81baf89a55ffd0e24b8f04a4185738dd3387` | 2026-08-24 |

SHAs were resolved with `git ls-remote` (annotated tags dereferenced to the
commit). No action is left with a `# TODO: pin SHA`.

## Tools

| Tool | Version | Released | Where |
|---|---|---|---|
| Trivy | v0.76.2 | 2026-11-05 | CI (`TRIVY_VERSION`) and local scans |
| GNU Make | 3.81+ | - | Makefile constraint (macOS `/usr/bin/make`) |
| Docker Compose | v2 | - | required by `gmake doctor` |

## Python - control plane runtime (`control-plane/requirements.txt`)

Installed with `pip install --uploaded-prior-to 2026-11-06T00:00:00Z` while
resolving, then frozen to exact versions (transitive dependencies included).

| Package | Version | Released |
|---|---|---|
| fastapi | 0.162.0 | 2026-10-23 |
| starlette | 1.10.0 | 2026-10-24 |
| pydantic | 2.14.3 | 2026-11-05 |
| pydantic_core | 2.50.4 | (pydantic pin) |
| uvicorn | 0.54.0 | 2026-09-25 |
| httpx | 0.28.1 | 2024-12-06 |
| prometheus_client | 0.26.0 | 2026-07-24 |
| annotated-doc 0.0.5, annotated-types 0.8.0, anyio 4.16.0, certifi 2026.10.23, click 8.5.0, h11 0.16.0, httpcore 1.0.9, idna 3.20, opentelemetry-api 1.45.2, typing-inspection 0.4.5, typing_extensions 4.16.0 | transitive | resolved under the same cut-off |

## Python - development (`control-plane/requirements-dev.txt`)

pytest 9.2.0 (2026-10-27), pytest-cov 7.1.0, coverage 7.17.2, ruff 0.17.4
(2026-11-05), mypy 2.4.1 (2026-10-28), yamllint 1.38.0, PyYAML 6.0.3, and their
transitive pins (iniconfig 2.3.1, packaging 26.3, pluggy 1.7.0, Pygments 2.21.0,
mypy_extensions 1.2.0, ast_serialize 0.13.1, librt 0.16.0, pathspec 1.1.1).

## Python - workload (`workload-template/requirements.txt`)

psycopg 3.3.8 and psycopg-binary 3.3.8 (2026-11-04).

## CLI

`cli/envctl` uses the Python standard library only and targets Python >= 3.9
(checked in CI on 3.9 and 3.13).

## Excluded (published after 2026-11-06)

| Component | Version | Published | Reason |
|---|---|---|---|
| Traefik | v3.7.18 | Docker Hub 2026-11-11 | < 14 days |
| Traefik | v3.7.19 | Docker Hub 2026-11-19 | < 14 days |
| Postgres | 17.12-alpine | Docker Hub 2026-11-19 | < 14 days |
| PyPI packages | any release after 2026-11-06 | - | excluded by `--uploaded-prior-to` |

## Not verified

- Exact publication dates of transitive PyPI packages were not looked up one by
  one; they are guaranteed older than the cut-off by `--uploaded-prior-to`.
- The image digests were resolved with `docker buildx imagetools inspect`
  (multi-arch index). Only `linux/arm64` (macOS) has been exercised so far;
  `linux/amd64` is exercised by the CI `e2e` job once it runs on GitHub.
