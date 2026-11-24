# syntax=docker/dockerfile:1

# ---- Build stage: resolve the locked dependencies and install the project into a venv with uv.
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, so this layer is reused while only the source changes.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- Runtime stage: slim Python with the venv only, running as a non-root user.
FROM python:3.12-slim AS runtime

# Apply Debian security updates released after the base image was built (the image scan fails on
# fixable HIGH/CRITICAL findings), and drop the base image's pip: the service runs from its own
# venv and never installs anything at run time.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y -q pip \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8002

# Every other setting (gateway URLs, IDENTITY_JWKS_URL, DATABASE_URL, KAFKA_BROKERS...) comes from
# the environment at run time; see README "Configuration". No secret is baked into the image.

USER app
EXPOSE 8002

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT', '8002')}/health\", timeout=3)"]

# The entrypoint applies the database migrations (a failure is logged, never fatal), then runs CMD.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "ecommerce_ai"]
