FROM debian:bookworm-slim

ARG FPKG_VERSION=2.2.6
ARG FPKG_SHA256=6a129c23a975e40d08d4a5daae5ad7e625992726e901b40b20ed1aa9c480e910
ARG SEVENZIP_URL=https://github.com/ip7z/7zip/releases/download/25.01/7z2501-linux-x64.tar.xz
ARG SEVENZIP_SHA256=4ca3b7c6f2f67866b92622818b58233dc70367be2f36b498eb0bdeaaa44b53f4

LABEL org.opencontainers.image.title="fpkg-webui" \
      org.opencontainers.image.description="Web UI for building PS5 FPKGs with PSVIETHOA fpkg-cli (archives, exFAT images, fixes)" \
      org.opencontainers.image.source="https://github.com/cosmicflow2512/fpkg-webui"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils libicu72 libssl3 python3 \
 && rm -rf /var/lib/apt/lists/*

# PSVIETHOA fpkg-cli (CLI only, the desktop GUI is removed)
RUN curl -fsSL -o /tmp/f.tgz "https://github.com/thanhsondev/PSVIETHOA-FPKG-Builder/releases/download/v${FPKG_VERSION}/PSVIETHOA-FPKG-Builder-${FPKG_VERSION}-Linux-x64.tar.gz" \
 && echo "${FPKG_SHA256}  /tmp/f.tgz" | sha256sum -c - \
 && mkdir -p /opt/fpkg && (tar xzf /tmp/f.tgz -C /opt/fpkg 2>/dev/null || true) \
 && test -f /opt/fpkg/fpkg-cli/fpkg-cli \
 && find /opt/fpkg -name '._*' -delete && rm -rf /tmp/f.tgz /opt/fpkg/app \
 && chmod +x /opt/fpkg/fpkg-cli/fpkg-cli \
 && ln -s /opt/fpkg/fpkg-cli/fpkg-cli /usr/local/bin/fpkg-cli

# official 7-Zip (static build, includes RAR)
RUN curl -fsSL -o /tmp/7z.tar.xz "${SEVENZIP_URL}" \
 && echo "${SEVENZIP_SHA256}  /tmp/7z.tar.xz" | sha256sum -c - \
 && mkdir -p /tmp/7z && tar xJf /tmp/7z.tar.xz -C /tmp/7z \
 && install -m 0755 /tmp/7z/7zzs /usr/local/bin/7z \
 && rm -rf /tmp/7z /tmp/7z.tar.xz

COPY app /app
RUN mkdir -p /shares /output /work /config

ENV PORT=8095 \
    BROWSE_ROOTS=/shares/*:/output:/work \
    DEFAULT_OUT=/output \
    DEFAULT_WORK=/work \
    DATA_DIR=/config \
    HOME=/config \
    PUID=99 PGID=100 \
    LOG_LEVEL=INFO \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

EXPOSE 8095
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD python3 -c "import urllib.request,sys;urllib.request.urlopen('http://127.0.0.1:8095/api/config',timeout=5)" || exit 1
CMD ["python3", "/app/server.py"]
