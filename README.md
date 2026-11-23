# News API

FastAPI backend for the news platform: Python 3.13, async SQLAlchemy 2, Alembic, PostgreSQL 18.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync                      # creates .venv with the locked dependencies (Python 3.13)
cp .env.example .env         # then set real passwords
uv run pre-commit install
docker compose up -d db      # PostgreSQL on localhost:5433
uv run alembic upgrade head  # schema + Hespress seed
docker compose exec -T db psql -U news -d news -v ON_ERROR_STOP=1 < scripts/db_roles.sql
```

`scripts/db_roles.sql` creates the least-privilege roles `news_ingest` (read/write) and
`news_api` (read-only). It is idempotent: re-run it after every migration.

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
| `GET /api/v1/articles` | Newest first. Filters: `source`, `category`, `language`, `published_after`, `published_before`, `q` (full-text). Paginate with `cursor` (from `next_cursor`) and `limit` (max 100). |
| `GET /api/v1/articles/{id}` | One article: metadata, link to the original, source attribution |
| `GET /api/v1/sources` | Publishers and their rights policy |
| `GET /api/v1/feeds` | Feed health, checkpoints, uncovered windows |

- **Read-only:** the API never writes. Its database connections are opened read-only, so
  PostgreSQL itself rejects writes. Optionally set `API_DATABASE_URL` to a login user in
  the `news_api` role (see `scripts/db_roles.sql`) for a second, privilege-level layer.
- **Caching:** responses carry an `ETag` with `Cache-Control: no-cache`. Send
  `If-None-Match` to get a cheap `304` when nothing changed.
- **Rights:** article bodies are never served. Every article links to the original and
  names its source.

Docs (`/docs`, `/redoc`, `/openapi.json`) are disabled when `ENVIRONMENT=production`.

## Ingest articles

Run this whenever you like (there is no scheduler). It collects everything published
since the last *completed* run, however long ago that was:

```bash
docker compose up -d db
uv run news ingest                  # first run: the last 30 days (--since-days to change)
uv run news ingest -v               # with progress logs
uv run news ingest --help
```

- **Safe to interrupt** (Ctrl-C, a network drop, closing the laptop): finished pages are
  kept and the next run resumes the gap.
- **One run at a time:** a second concurrent run is refused.
- **Polite:** about 5 s between requests to a publisher, per its robots.txt. One week of
  articles takes about 2 minutes; months take longer (capped by `--max-pages`, and the
  next run continues).
- **Exit codes:**

  | Code | Meaning |
  | --- | --- |
  | 0 | ok |
  | 1 | some feeds failed (others still ran) |
  | 2 | not ready (database down or not migrated) |
  | 3 | already running |
  | 130 | interrupted |

- **Feed health:** each run is recorded in `ingestion_runs`. After 5 consecutive failures
  a feed is marked `degraded`; after 20 it is `disabled` and skipped. To re-enable it:
  `UPDATE feeds SET state = 'active', consecutive_failures = 0 WHERE id = …;`

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
uv run alembic upgrade head       # then re-run scripts/db_roles.sql (see Setup)
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
