# syntax=docker/dockerfile:1

# ── build ────────────────────────────────────────────────────────────────────
# Dependencies are installed from uv.lock in a stage that is thrown away, so
# neither uv nor any build tooling ends up in the runtime image.
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Lockfile first, source second: editing a .py file then only re-runs the
# final COPY instead of reinstalling every dependency.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

COPY . .

# ── runtime ──────────────────────────────────────────────────────────────────
FROM python:3.13-slim

# Non-root. This process only makes outbound HTTP calls and serves a port —
# it never needs to write to disk.
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --no-create-home app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=9000

USER app

# Documentation only — EXPOSE publishes nothing. It records the default; if
# you override MCP_PORT, publish and probe that port instead.
EXPOSE 9000

# Probes /healthz (plain HTTP) rather than /mcp — kubelet and Docker cannot
# speak JSON-RPC. Returns 503 when the upstream API is unreachable, so an
# unhealthy container points at the real cause. Reads MCP_PORT so it follows
# the port the app actually bound.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; port=os.environ.get('MCP_PORT','9000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4).status == 200 else 1)"]

# Exec form, no shell: uvicorn runs as PID 1 and receives SIGTERM directly, so
# Kubernetes gets a graceful shutdown instead of waiting out the grace period.
# Goes through server.py's __main__ so MCP_HOST/MCP_PORT actually take effect.
CMD ["python", "server.py"]
