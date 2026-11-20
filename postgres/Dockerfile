# Per-environment database image: the pinned upstream Postgres plus Alpine
# security fixes that are already published but not yet in an upstream tag
# older than 14 days. See docs/VERSIONS.md and .trivyignore.yaml.
FROM postgres:17.11-alpine@sha256:e5d2edd28b1f8d40a0a6c0c9cdc545b19b56a0d5d88f94bc11a97d970d4700d0
LABEL org.opencontainers.image.title="ephemeral-idp postgres" \
      org.opencontainers.image.source="https://github.com/Nova20004/self-service" \
      org.opencontainers.image.licenses="MIT"
# gosu is only used by the entrypoint to drop root; this image always runs
# as uid 70 (set by the provisioner), so the binary is dead weight and is
# removed (it also carries an old Go toolchain flagged by scanners).
RUN apk add --no-cache krb5-libs=1.22.3-r0 \
 && rm -f /usr/local/bin/gosu
USER 70:70
HEALTHCHECK --interval=5s --timeout=3s --retries=5 \
  CMD ["pg_isready", "-h", "127.0.0.1", "-U", "app", "-d", "app"]
