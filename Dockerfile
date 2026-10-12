# syntax=docker/dockerfile:1.7
# Production image for la-goyniela-api, deployable to Railway or any
# OCI-compatible host. Uses uv for fast, reproducible installs.
#
# Build:  docker build -t la-goyniela-api .
# Run:    docker run --rm -p 8000:8000 --env-file .env la-goyniela-api

FROM python:3.12-slim AS base

# System deps for psycopg + healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Set working dir
WORKDIR /app

# Layer 1: deps only (cached when pyproject.toml doesn't change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Layer 2: source
COPY app ./app
COPY db ./db
COPY alembic.ini ./
COPY scripts ./scripts
COPY data ./data
COPY readme.md ./

# Layer 3: install the project itself (no-op for our lib-less layout)
RUN uv sync --frozen --no-dev

# Drop privileges (good practice; Railway runs as non-root by default)
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8000

# Healthcheck uses the FastAPI /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Entrypoint: run migrations, then start the app. The release step is
# idempotent — `alembic upgrade head` is a no-op when the DB is at head.
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2"]
