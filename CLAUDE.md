# CLAUDE.md

Personal news-intelligence platform. An ingestion command (`news ingest`), run daily by
GitHub Actions or by hand, collects articles from publishers into PostgreSQL on **Neon**. A read-only FastAPI API serves
them to a web frontend. Content analysis comes later. First source: **Hespress English**
(`https://en.hespress.com`).

Constraints: **$0 infrastructure** (no paid services; no free tier as a dependency
except **Neon**, owner decision 2026-11-25, architecture section 5.6), and the Mac is
**not always online**. The approved design,
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
docker compose up -d db                   # local TEST Postgres on localhost:5433 (the app uses Neon)
uv run uvicorn app.main:app --reload      # API (port 8000; may be taken locally)
uv run ruff check --fix . && uv run ruff format .
uv run mypy app tests alembic
uv run pytest -m "not integration"        # fast, no DB
uv run pytest                             # full suite: local db container + TEST_DATABASE_URL
uv run alembic revision --autogenerate -m "..." && uv run alembic upgrade head
uv run alembic check                      # models and migrations must be in sync
uv run news ingest [-v] [--since-days N] [--max-pages N] [--no-sitemap] [--rewalk-days N]
                                          # hits real publishers: only when asked
                                          # --rewalk-days re-reads a window for bodies;
                                          # it never moves the checkpoint
uv run news backup && uv run news restore-test   # dump to ~/Backups/news, then prove it
uv run news analyze [--limit N] [--concurrency N] [--retry-failed]
                                          # Gemini API, parallel, resumable, within limits
uv run news lab <ids...> [--label X]      # Gemini API: max 3 answered requests per test
uv run news gemini-usage                  # API use vs our limits and Google's
uv run news lab-compare <run dirs...>     # score lab runs with the current code checks
uv run news eval export [--n 30]          # gold sample to ~/news-eval/gold (outside repo)
uv run news eval score <labels.csv>       # precision per section + consistency; no API
uv run python scripts/check_repo_hygiene.py   # secrets, data files, article quotes
```

**Definition of done:** ruff, mypy and the full pytest suite pass, `alembic check` is clean,
and new behavior has tests. CI (`.github/workflows/ci.yml`) runs exactly these.

## Layout

```
app/
  cli.py          the `news` command: ingest, analyze, lab, lab-compare, eval,
                  gemini-usage, backup, restore-test
  main.py         app factory + lifespan (engine created/disposed here, never at import;
                  startup privilege guard); mounts /ui
  analysis/       Gemini analysis: budget (RequestBudget, CEILINGS), ledger (api_calls),
                  llm, pipeline, prompt, schema, evidence, cleanup, briefing, lab,
                  compare, eval
  api/            health.py (unversioned probes), deps.py, caching.py,
                  v1/ (catalog.py, analysis.py: all business routes)
  core/           config (pydantic-settings), JSON logging, request-ID middleware,
                  error envelope
  db/             Base with naming convention, async engine/session factories,
                  assert_least_privilege
  ingestion/      fetch (polite fetcher), robots, parsers (defusedxml), policy (rights
                  gate), normalize, sources/wordpress.py, walk, store, readiness, runner
  models/         SQLAlchemy models; import every model module in models/__init__.py
  ops/            backup.py (news backup, news restore-test)
  schemas/        Pydantic response schemas (catalog, analysis briefings)
  web/static/     the /ui briefing screen (HTML, CSS, JavaScript; no build step)
alembic/          async migrations
scripts/          db_roles.sql, db_login_passwords.sql, check_repo_hygiene.py
tests/            unit tests; tests/integration/ needs PostgreSQL (marker: integration)
```

The archive backlog (milestone 6) is deferred. Ingestion is a
**run-to-completion command**, not a daemon. It runs daily from
`.github/workflows/ingest.yml` (GitHub-hosted, against Neon, architecture 5.6) or by
hand. No launchd, no self-hosted runner. The workflow runs `news ingest` only, with the
`INGEST_DATABASE_URL` secret (`news_ingest_login`, Neon's direct endpoint,
`sslmode=require`); never tests, migrations or Gemini analysis.

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
  - Every request goes through `RequestBudget`. Limits (the same numbers in
    `docs/architecture.md` 5.5 and `.env.example`): 20 requests per minute; 10,000 input
    tokens per minute; 1,000 requests per day; 3 answered requests per `news lab` test;
    concurrency default 8. The per-minute and token limits are at least one third below
    Google's; the daily limit is stricter. Settings can only lower them. Never raise the
    ceilings, widen the model allowlist, retry anything but Google's temporary overloads
    (503/500/502/504/timeouts: retried with the same model until answered), add billable
    request options, or call the real API from tests, without the owner's explicit
    decision. Check use with `uv run news gemini-usage`.
  - How the limits are enforced (`app/analysis/budget.py`, `ledger.py`, `llm.py`): the
    lab ceiling of 3 and the concurrency ceiling of 8 are constants (`CEILINGS`); the
    per-minute window is shared across processes via `api_calls` (a client seeds it from
    the last 60 s of rows before its first request); input tokens are estimated as
    UTF-8 bytes // 3 (+1) before sending.
  - AI outputs meant for publication must be original work: no reproduced or
    closely paraphrased passages.
- **Geography: use Morocco GeoNames datasets only.** Never mention, link to, or map the
  disputed southern territories or the places governed under them, in product code,
  documentation, tests, UI or commit messages. Do not write the disputed territory's name
  anywhere in the repo, not even to describe this rule. This is an owner rule (2026-11-24).
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
  - Roles (`scripts/db_roles.sql`, re-run after each migration and after a restore): the
    database owner (`neondb_owner` on Neon) owns the schema and runs migrations; `news_ingest` reads and writes;
    `news_api` is SELECT-only on an explicit allowlist (`articles`, `feeds`, `sources`,
    the `analysis_briefings` view), never on `article_bodies`, `article_analyses`,
    `api_calls` or `analysis_runs`. Both are NOLOGIN groups; the login roles are
    `news_api_login` and `news_ingest_login`, whose passwords are set by
    `scripts/db_login_passwords.sql` from psql variables (never written in a file).
  - The API's engine is `create_engine(settings, read_only=True)`: its connections set
    `default_transaction_read_only=on`, and it uses `API_DATABASE_URL` (a
    `news_api_login` URL) when set. Never give the API a read-write engine.
  - Startup privilege guard: `assert_least_privilege` (`app/db/session.py`, called from
    the lifespan) refuses to start in production, and warns elsewhere, if the API's login
    is a superuser or can SELECT `article_bodies`. Never weaken it.
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
- **Infrastructure.** Nothing may require a paid service or a free-tier account,
  except Neon (5.6). The only internet-reachable PostgreSQL is Neon (TLS + passwords);
  the local container stays on localhost. `ci.yml` has no secrets and never touches real
  data; `ingest.yml` is the only workflow with real data, guarded by
  `tests/test_workflows.py`. Migrations and `scripts/db_roles.sql` are run by the owner.
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

- The app's database is **Neon** (`DATABASE_URL`, direct endpoint, never `-pooler`). The
  local container on **5433** (5432 belongs to another project) is for tests only:
  `tests/conftest.py` refuses a Neon URL, so set `TEST_DATABASE_URL`. Migrations and
  `scripts/db_roles.sql` against Neon are run from the Mac.
- Port 8000 may be taken by a local uvicorn. Use `--port 8001` or
  `API_PORT=8001 docker compose up -d api`.
- The VS Code GitHub Actions extension shows false "unable to resolve action" errors.
  Verify tags with `gh api repos/<owner>/<repo>/tags` instead. `setup-uv` has no floating
  major tag, so it's pinned by SHA.
