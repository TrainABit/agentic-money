# Sovereign engine image. Build from the repository root:
#   docker build -t sovereign .
# The data directory is a volume; the image never bakes secrets or master.key.
FROM python:3.12-slim-bookworm AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY sovereign ./sovereign
RUN pip install --no-cache-dir build==1.5.0 \
    && python -m build --wheel

FROM python:3.12-slim-bookworm

RUN useradd --system --uid 10001 --create-home --home-dir /data sovereign
WORKDIR /app
COPY --from=build /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl \
    && rm -f /tmp/*.whl

USER sovereign
VOLUME ["/data"]
ENV SOVEREIGN_DATA_DIR=/data
ENV SOVEREIGN_MODE=sim
EXPOSE 7474

# Readiness by default. Orchestrators that want liveness should pass
# --stale-seconds (see deploy/docker-compose.yml).
HEALTHCHECK --interval=30s --timeout=20s --start-period=90s --retries=3 \
    CMD sovereign healthcheck --data-dir /data || exit 1

ENTRYPOINT ["sovereign"]
CMD ["serve", "--data-dir", "/data", "--mode", "sim"]
