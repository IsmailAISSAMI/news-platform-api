# CLAUDE.md

Personal news-intelligence platform. A manually run ingestion command (`news ingest`)
collects articles from publishers into a local PostgreSQL. A read-only FastAPI API serves
them to a web frontend. Content analysis comes later. First source: **Hespress English**
(`https://en.hespress.com`).

Constraints: **$0 infrastructure** (local-first on the owner's Mac; no paid services; no
free tier as a dependency), and the Mac is **not always online**. The approved design,
verified source facts, rights model and roadmap are in
[docs/architecture.md](docs/architecture.md). Read it before working on ingestion, the
schema or the API. **Each roadmap milestone needs the owner's approval before it starts.**

## Stack

Python 3.13 (pinned in `.python-version`), uv, FastAPI, async SQLAlchemy 2 + psycopg 3,
Alembic, PostgreSQL 18, Docker Compose, pytest, ruff, mypy (strict), GitHub Actions.

## Commands

Always go through `uv run`; never `pip install` (dependencies are locked in `uv.lock`).

```bash
uv sync                                   # install locked deps
uv add <pkg> / uv add --dev <pkg>         # add a dependency (updates uv.lock)
docker compose up -d db                   # Postgres on localhost:5433
uv run uvicorn app.main:app --reload      # API (port 8000; may be taken locally)
uv run ruff check --fix . && uv run ruff format .
uv run mypy app tests alembic
uv run pytest -m "not integration"        # fast, no DB
uv run pytest                             # full suite, needs the db container
uv run alembic revision --autogenerate -m "..." && uv run alembic upgrade head
uv run alembic check                      # models and migrations must be in sync
uv run news ingest [-v] [--since-days N] [--max-pages N] [--no-sitemap]
                                          # hits real publishers: only when asked
uv run news backup && uv run news restore-test   # dump to ~/Backups/news, then prove it
uv run news analyze [--limit N] [--concurrency N] [--retry-failed]
                                          # Gemini API, parallel, resumable, within limits
uv run news lab <ids...> [--label X]      # Gemini API: max 3 answered requests per test
uv run news gemini-usage                  # API use vs our limits and Google's
```

**Definition of done:** ruff, mypy and the full pytest suite pass, `alembic check` is clean,
and new behavior has tests. CI (`.github/workflows/ci.yml`) runs exactly these.

## Layout

```
app/
  main.py         app factory + lifespan (engine created/disposed here, never at import)
  api/            health.py (unversioned probes), deps.py, v1/ (all business routes)
  core/           config (pydantic-settings), JSON logging, request-ID middleware, errors
  db/             Base with naming convention, async engine/session factories
  models/         SQLAlchemy models; import every model module in models/__init__.py
alembic/          async migrations
tests/            unit tests; tests/integration/ needs PostgreSQL (marker: integration)
```

Planned (see architecture doc section 10): `app/ingestion/` (rights gate, polite fetcher,
per-source adapters, checkpoint walk, archive backlog, CLI). Ingestion is a
**run-to-completion command run by hand**, not a daemon: no launchd, no scheduler, no
GitHub-hosted or self-hosted runner.

## Hard rules: content rights and polite crawling

These come from the source's terms of use. They are not style preferences. Changing any
of them requires an explicit decision by the owner, recorded in the architecture doc.

- Every source has a `rights_policy`: `disabled` | `metadata_only` | `restricted` |
  `licensed_full_text` (meanings in architecture doc section 5.3).
- **Hespress is `restricted` (owner decision, 2026-11-23; see architecture section 5.4).**
  - Full text is stored **privately** in `article_bodies` for non-commercial educational
    AI analysis, with the basis recorded in `sources.rights_basis`. No permission from
    Hespress was obtained, and that is recorded too.
  - Article text is **never served** by the API, exported, published or committed. The
    `news_api` role cannot read `article_bodies`.
  - **AI analysis runs on Gemma 4 31B via the Gemini API, free tier only (owner
    decision, 2026-11-24; architecture section 5.5)**: article text is sent to Google.
    LM Studio is retired. Billing stays off on the Google project.
  - Every request goes through `RequestBudget` (always 1/3 below Google's limits: 20 per minute,
    10,000 input tokens per minute; 1,000 per day; 3 answered per `news lab` test; settings can only
    lower them). Never raise the
    ceilings, widen the model allowlist, retry anything but Google's temporary
    overloads (503/500/502/504/timeouts: retried with the same model until answered), add billable
    request options, or call the real API from tests, without the owner's explicit
    decision. Check use with `uv run news gemini-usage`.
  - AI outputs meant for publication must be original work: no reproduced or
    closely paraphrased passages.
- Bodies leave the rights gate only under `restricted` (with a non-empty
  `rights_basis`) or `licensed_full_text` (valid license). Anything doubtful falls back
  to `metadata_only`. Don't change a source's policy without the owner's explicit
  decision.
- Never store raw feed payloads: they contain full text.
- Never assume that processing in memory, private use or paraphrasing makes full-text use
  lawful.
- The API and frontend always link out to the original article with attribution.
- **Never commit real article text** to this public repo. Test fixtures are synthetic.
- **Allowed inputs:**
  - RSS, including `?paged=N`;
  - the news sitemap;
  - monthly archive sitemaps;
  - article pages, for **metadata only** (`og:*`, `article:*`, JSON-LD) and only to
    resolve the archive backlog.
- **Fetch politely:**
  - Respect `robots.txt` (including the Disallow lines outside a group) and
    `Crawl-delay` (Hespress: 5 s).
  - Use conditional GET (Hespress returns 304).
  - Send the User-Agent from settings, with the repo URL as contact.
- Never bypass authentication, paywalls, anti-bot measures or access restrictions.

## Ingestion invariants

- **Readiness and locking.** Check the database is up (fail fast, about 10 s) and at the
  Alembic head, then take a `pg_try_advisory_lock`. One run at a time: no job queue, no
  leases, no `SKIP LOCKED`.
- **Walk to the checkpoint.** The RSS walk continues until it **sees** the feed's
  checkpoint article, or passes the checkpoint's date minus 1 day. Never stop just
  because a known article appeared: after an interrupted run that would skip the rest of
  the gap.
- **Move the checkpoint only on completion.** Commit after every page, but advance the
  checkpoint only when the walk finishes.
- **Don't order by ID.** WordPress post IDs are assigned at draft creation, not
  publication, so they're not in publish order.
- **Keep the archive backlog safe.** It is persistent (`discovered_urls`), bounded per
  run (`--max-pages`), idempotent, and records attempts. Don't promise zero loss beyond
  the RSS paging depth.
- Every run writes an `ingestion_runs` row and prints a report.

## Engineering conventions

- **Async all the way.** No blocking I/O in `async def` (ruff `ASYNC` rules enforce it).
  CPU-bound or sync-only work runs via `asyncio.to_thread`.
- **Database.**
  - Schema changes only through Alembic migrations. Never edit a migration that is
    already on `main`; add a new one.
  - Rely on the naming convention in `app/db/base.py`; don't hand-name constraints.
  - All timestamps are `timestamptz` and stored in UTC.
  - Dedup key per article: `UNIQUE(source_id, external_id)`. For Hespress, `external_id`
    is the WordPress post ID from `<guid>` (`?p=149695`).
  - Roles (`scripts/db_roles.sql`, re-run after each migration): the `POSTGRES_USER`
    owns the schema and runs migrations; `news_ingest` reads and writes; `news_api` is
    SELECT-only.
  - The API's engine is `create_engine(settings, read_only=True)`: its connections set
    `default_transaction_read_only=on`, and it uses `API_DATABASE_URL` (a `news_api`
    login) when set. Never give the API a read-write engine.
  - Seed or reference data goes in migrations, written idempotently
    (`ON CONFLICT DO NOTHING`).
- **API.**
  - Business routes live under `/api/v1` and are read-only (`GET` only).
  - Use keyset pagination on `(published_at, id)` with a bounded `limit`. Never `OFFSET`.
  - Response bodies are Pydantic schemas, never ORM objects.
- **Config.** Add new settings to `app/core/config.py` and document them in `.env.example`.
  Secrets are `SecretStr`.
- **Logging.** Use `logging.getLogger(__name__)`; never `print`. Log events with useful
  context, never secrets or full article bodies.
- **Tests.**
  - No live network calls. Mock HTTP with `httpx.MockTransport` and use fixture files.
  - Use the `db_session` fixture (rolled back per test) for DB tests, and mark them
    `integration`.
  - Tests run against a separate `<db>_test` database (`news_test`), never the real one:
    `tests/conftest.py` rewrites `DATABASE_URL` before the app is imported, and
    `tests/integration/conftest.py` creates and migrates it. Never weaken that guard.
    It is safe to run tests while a real `news ingest` runs (advisory locks are per DB).
- **Typing.** mypy strict must pass. A `# type: ignore` needs an error code and a reason.
- **Dependencies.** Prefer the standard library and existing deps. Justify every new one.
  No Celery, Redis, Kafka, Kubernetes or Elasticsearch. Parse all XML (RSS, sitemaps)
  with `defusedxml` through `app/ingestion/parsers.py`: strict, so malformed input fails
  loudly. No `feedparser`.
- **Ingestion tests** use the simulated publisher in `tests/integration/test_walk.py` and
  the synthetic builders in `tests/ingestion/feeds.py`. Never use real feed content.
- **Infrastructure.** Nothing may require a paid service or a free-tier account.
  PostgreSQL is never exposed to the internet. GitHub Actions is CI only, and never
  touches real data.
- **Backups.** Dumps go to `~/Backups/news/`, outside the repo. A backup only counts once
  a restore test has passed.

## Git

- Branch is `main`. Use Conventional Commits in English: `feat:`, `fix:`, `refactor:`,
  `test:`, `docs:`, `build:`, `ci:`.
- pre-commit runs ruff, mypy and secret checks. Don't bypass it with `--no-verify`.
- Never commit `.env`. `.env.example` holds placeholders only, and must work when copied
  as-is.
- Never commit data: backups (`backups/`, `*.dump`, `*.sql.gz`) are ignored. Plain `*.sql`
  is deliberately **not** ignored, because SQL source files such as
  `scripts/db_roles.sql` belong in git.
- **Never read `.env`** or print secrets.
- Don't delete Docker volumes or databases.
- Don't commit or push without the owner's explicit approval.

## Local environment gotchas

- **The project is in an iCloud-synced folder (`~/Desktop`).** iCloud corrupts virtualenvs
  by creating "`file 2.py`" conflict copies.
  - The real venv is `.venv.nosync` (iCloud ignores `*.nosync`), and `.venv` is a symlink
    to it.
  - Never recreate `.venv` as a plain directory.
  - If imports break mysteriously, look for `* 2*` files.

- Port 5432 belongs to another project's Postgres. This project uses **5433**.
- Port 8000 may be taken by a local uvicorn. Use `API_PORT=8001 docker compose up -d`.
- The VS Code GitHub Actions extension shows false "unable to resolve action" errors.
  Verify tags with `gh api repos/<owner>/<repo>/tags` instead. `setup-uv` has no floating
  major tag, so it's pinned by SHA.
