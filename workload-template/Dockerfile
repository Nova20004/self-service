FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-compile -r /tmp/requirements.txt \
 && /opt/venv/bin/pip uninstall --yes pip

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
LABEL org.opencontainers.image.title="ephemeral-idp workload template" \
      org.opencontainers.image.source="https://github.com/Nova20004/self-service" \
      org.opencontainers.image.licenses="MIT"
# zlib1g: Debian security fix (CVE-2026-85091) newer than the base image.
# pip is removed from the runtime image: it is not needed and vendors
# packages (urllib3, msgpack) that scanners flag.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall --yes --root-user-action=ignore pip \
 && groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /opt/venv /opt/venv
COPY main.py /srv/main.py
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
USER 10001:10001
WORKDIR /srv
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["python", "/srv/main.py"]
