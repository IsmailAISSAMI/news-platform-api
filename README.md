# News API

FastAPI backend for the news platform: Python 3.13, async SQLAlchemy 2, Alembic, PostgreSQL 18.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync                      # creates .venv with the locked dependencies (Python 3.13)
cp .env.example .env         # then set real passwords
uv run pre-commit install
docker compose up -d db      # PostgreSQL on localhost:5433
uv run alembic upgrade head
```

## Run

```bash
uv run uvicorn app.main:app --reload    # http://localhost:8000 (docs at /docs)
docker compose up --build               # db + migrations + api, all in containers
API_PORT=8001 docker compose up -d      # if port 8000 is taken
```

Endpoints:

| Path                 | Purpose                                      |
| -------------------- | -------------------------------------------- |
| `GET /health/live`   | Liveness: the process is up                  |
| `GET /health/ready`  | Readiness: the database is reachable (or 503) |
| `/api/v1/...`        | Versioned business API                       |

Docs (`/docs`, `/redoc`, `/openapi.json`) are disabled when `ENVIRONMENT=production`.

## Quality checks

CI runs the same commands.

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy app tests alembic
uv run pytest                      # unit + integration (needs the db container)
uv run pytest -m "not integration" # unit tests only
```

## Migrations

```bash
uv run alembic revision --autogenerate -m "describe change"
uv run alembic upgrade head
uv run alembic check               # fails if models and migrations are out of sync
```

Import every new model module in `app/models/__init__.py` so autogenerate sees it.

## Dependencies

Versions are locked in `uv.lock`; commit it. Use `uv add <pkg>` / `uv add --dev <pkg>`,
and `uv lock --upgrade` to update. Dependabot opens weekly update PRs.

## Configuration

All settings come from environment variables or `.env` (see `.env.example`).
`DATABASE_URL` is required and must use the `postgresql+psycopg://` driver.

## Layout

```
app/
  main.py        app factory + lifespan (engine start/stop)
  api/           routers: health (unversioned), v1/ (business API), deps
  core/          settings, logging (JSON), request-ID middleware, error handlers
  db/            declarative base (naming convention), async engine/session
  models/        SQLAlchemy models
alembic/         async migrations
tests/           unit tests; integration/ needs PostgreSQL
```
