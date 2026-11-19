# Turf API image. Two stages: the builder installs dependencies with uv; the
# runtime image gets only the finished virtualenv (no uv, no download cache, no
# pipeline-only packages like numpy). Data is not baked in: it's mounted
# read-only at /data, so a data refresh needs no rebuild.

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first so code-only changes reuse this layer. --no-default-groups
# leaves out dev tools and the pipeline group.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-default-groups --no-install-project
COPY src ./src
# --no-editable installs turf_app into the venv, so the runtime stage doesn't need src/.
RUN uv sync --frozen --no-default-groups --no-editable

FROM python:3.12-slim
RUN useradd --system --uid 10001 turf
COPY --from=builder /app/.venv /app/.venv
USER turf

# TURF_CLIENT_IP_HEADER: rate limiting keys on X-Client-IP, which Caddy sets
# (replacing anything a visitor sends) from Cloudflare's CF-Connecting-IP.
# X-Forwarded-For is not trusted: Cloudflare passes through what visitors put in it.
ARG TURF_COMMIT=dev
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TURF_DB=/data/turf.duckdb \
    TURF_COMMIT=$TURF_COMMIT \
    TURF_CLIENT_IP_HEADER=X-Client-IP
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["uvicorn", "turf_app.api:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--no-proxy-headers"]
