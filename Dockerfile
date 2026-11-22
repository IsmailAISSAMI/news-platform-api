# syntax=docker/dockerfile:1

# ---- build stage: resolve and install locked dependencies into a venv ----
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /srv

# Dependencies first: this layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Then the application itself, installed (non-editable) into the venv.
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- runtime stage: slim image, venv only, non-root ----
FROM python:3.13-slim AS runtime

ENV PATH="/srv/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

RUN useradd --system --create-home --uid 1000 appuser

COPY --from=builder --chown=appuser:appuser /srv/.venv /srv/.venv
# Migrations are run as a separate one-off command (see docker-compose.yml).
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser alembic ./alembic

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
