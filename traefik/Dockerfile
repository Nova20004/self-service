FROM traefik:v3.7.17@sha256:f4bb143d1135a579b630ed491e17ec50f6f7c9c6a30585ff3d385471d2a86f5a
LABEL org.opencontainers.image.title="ephemeral-idp edge (traefik)" \
      org.opencontainers.image.source="https://github.com/Nova20004/self-service" \
      org.opencontainers.image.licenses="MIT"
COPY traefik.yml /etc/traefik/traefik.yml
USER 65534:65534
EXPOSE 8081
# Not `traefik healthcheck`: re-executing the 160 MB binary every 5 s inside a
# 64 MB cgroup thrashes the page cache and starves the proxy itself.
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
  CMD ["wget", "-q", "--spider", "http://127.0.0.1:8082/ping"]
