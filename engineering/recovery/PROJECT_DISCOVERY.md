# Project discovery (2026-11-25)

Written from the code, configuration and git history at commit `3274239`. Where a fact
could not be verified it says so.

## What the system is

A personal news-intelligence platform for one owner. It collects articles from one
publisher (Hespress English), stores them in PostgreSQL, analyses them with an LLM into
structured briefings, and serves article metadata and briefings through a read-only API
and a small web screen. Content rights are a first-class constraint: article text is
stored privately and never served (`docs/architecture.md` 5.3–5.4).

## Technology stack

| Layer | Technology |
| --- | --- |
| Language | Python 3.13 (`.python-version`), package manager uv (`uv.lock`) |
| Web | FastAPI + uvicorn, static UI (HTML/CSS/JS, no build step) under `/ui` |
| Data | SQLAlchemy 2 async + psycopg 3, Alembic (11 migrations), PostgreSQL 18 |
| Database host | Neon (free plan, Frankfurt), one `DATABASE_URL` (owner decision 2026-11-25) |
| HTTP client | httpx; XML through defusedxml |
| LLM | Gemma 4 31B via the Gemini API (OpenAI-compatible endpoint), free tier only |
| Tests | pytest, pytest-asyncio, pytest-cov (branch, ≥95 %), pytest-randomly |
| Quality | ruff, mypy strict, pre-commit (incl. gitleaks and a repo-hygiene script) |
| Runtime packaging | Dockerfile; docker-compose (`db` = local test PostgreSQL, `api` optional) |
| Automation | GitHub Actions: `ci.yml` (checks), `ingest.yml` (daily ingest at 22:47 UTC) |

Runtime dependencies (8): fastapi, uvicorn[standard], pydantic-settings,
sqlalchemy[asyncio], alembic, psycopg[binary], httpx, defusedxml.

## Entry points

- **CLI** `news` (`app/cli.py:main`, `[project.scripts]`): `ingest`, `analyze`, `lab`,
  `lab-compare`, `gemini-usage`, `eval export|score`, `backup`, `restore-test`.
- **API** `app.main:app` (FastAPI factory `create_app`; lifespan creates the read-only
  engine and runs the least-privilege check).
- **Migrations** `alembic/env.py` (async), URL from `Settings.database_url`.
- **Workflows** `.github/workflows/ci.yml`, `.github/workflows/ingest.yml`.

## Structure (sizes in lines of Python)

- `app/ingestion/` — polite fetcher (robots, delay, conditional GET, redirects), strict XML
  parsers, rights gate (`policy.py`), WordPress source adapter, checkpoint walk, store,
  readiness (DB up + Alembic head), runner (advisory lock, run report).
- `app/analysis/` — Gemini client (`llm.py`, 358), request budget and ledger
  (`budget.py`, `ledger.py`), prompt/schema, evidence checks (`evidence.py`, 343),
  cleanup and originality filter (`cleanup.py`, 308), briefing builder, pipeline (507),
  lab, compare, eval (336).
- `app/api/` — health probes, `v1/catalog.py` (sources, feeds, articles: keyset
  pagination, filters, full-text search, ETag), `v1/analysis.py` (briefings via the
  `analysis_briefings` view).
- `app/db/`, `app/core/` — engine/session factories, privilege guard, settings, JSON
  logging, request-ID middleware, error envelope.
- `app/ops/backup.py` (413) — snapshot-consistent `pg_dump`, manifest, restore test.
- `app/cli.py` (598) — all commands.
- `tests/` — 734 tests (576 unit, 158 integration against a local `<db>_test`).

Total: `app/` 7,777 lines; `tests/` 8,391 lines; docs about 1,700 lines.

## Critical boundaries

1. **Rights gate** — `app/ingestion/policy.py` is the only path from a fetched item to a
   storable record; bodies are stored only under `restricted` (with a basis) or
   `licensed_full_text`.
2. **Article text never leaves through the API** — routes read only `articles`, `feeds`,
   `sources` and the `analysis_briefings` view; the API engine is read-only.
3. **Gemini quota** — every request passes `RequestBudget` with constant ceilings; a
   database ledger (`api_calls`) paces across processes.
4. **Ingestion correctness** — one run at a time (advisory lock), walk back to the
   checkpoint, checkpoint moves only on completion.
5. **Test isolation** — `tests/conftest.py` rewrites the database to `<db>_test` and
   refuses a Neon address.

## Build and test commands

See `BASELINE.md` (all green at baseline): `uv sync --locked`, `ruff check`,
`ruff format --check`, `mypy app tests alembic`, `alembic check`,
`scripts/check_repo_hygiene.py`, `pytest` (needs `docker compose up -d db` and
`TEST_DATABASE_URL`), `docker build .`, `pre-commit run --all-files`.

## Known concerns going in (to be confirmed or rejected by the audits)

- **History of AI-driven rounds**: three audit-and-fix rounds, then the Neon migration,
  all on 2026-11-23..25. Expect comment and documentation layering, and tests adjusted
  during those rounds.
- **Recent owner simplification** (2026-11-25): one `DATABASE_URL` everywhere; the
  restricted login roles, `API_DATABASE_URL` and the privilege guard remain in code as
  optional features. Whether that leftover machinery is still worth its complexity is an
  open question for the owner.
- **Archive backlog (milestone 6)** is deferred, but the `discovered_urls` table and
  references to it exist.
- `pyproject.toml` still describes the project as "Enterprise-grade FastAPI backend".
- The repository lives in an iCloud folder; conflict copies appear locally.
- Not verified here: production deployment of the API (none exists; it runs locally on
  demand), and the first scheduled run of `ingest.yml` (due 22:47 UTC on 2026-11-25).
