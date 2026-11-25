# Integration audit (2026-11-25, read-only)

Scope: API routes / schemas / `/ui` JS; `analysis_briefings` + `briefing.py` vs `BriefingOut`;
models vs migrations vs CHECKs; Settings vs `.env.example` vs docs vs compose; Gemini client
(`llm.py`, code only); parsers vs synthetic fixtures; Neon assumptions; ingest workflow env.
No `.env`, no Neon, no pytest/Gemini/ingest. One introspection command was run (below).

## Findings

### IA-001 · contract drift · P1 · app/analysis/briefing.py:139-142 vs app/schemas/analysis.py:18,42,77
The originality filter sets a copied scalar outside any list to `null`
(`node[last] = None`). `BriefingOut` declares `overview.headline`, `what.main_event`,
`tone.label`, `tone.explanation` (and `overview.report_type/event_status`) as required `str`.
A briefing whose headline or tone explanation shares 8 tokens with the article is stored, then
fails validation: the list silently skips it and the detail returns 404 (`analysis.py:155-164`).
- Evidence: `uv run python -c` with `make_briefing(a, [], paragraphs=[p])`, where headline and
  tone.explanation equal `p` → `{'headline': None ...} {'explanation': None}` →
  `BriefingOut.model_validate` → `INVALID 2` errors.
- Impact: analysed articles disappear from the UI with only a warning log. `main_event` (a
  one-sentence summary) is the field most likely to hit 8 shared tokens.
- Risk of change: low. Make these fields `str | None` in `BriefingOut` (the UI already falls
  back to `d.title` / "not stated"), or have the filter drop the whole briefing explicitly
  with status `needs_review`. The frozen copy in migration `aa372fadc9eb` has the same rule.
- Verify: a unit test that makes a briefing with a copied headline and round-trips it
  through `BriefingOut`; count stored briefings that fail `_parse` (read-only query).

### IA-002 · config/owner-decision conflict · P1 · app/main.py:32-34, app/db/session.py:77, docs/architecture.md 5.6
Owner decision 5.6: "the Mac, the API and GitHub all use `DATABASE_URL`"; the separate logins
are not used. But the API lifespan refuses to start when `ENVIRONMENT=production` and
`API_DATABASE_URL` is unset, and `assert_least_privilege` refuses an owner login that can read
`article_bodies` (neondb_owner can). `docker-compose.yml:30-31` passes only `DATABASE_URL` and
`ENVIRONMENT`. So a production API is impossible under the recorded decision; in development
it only warns. CLAUDE.md (roles, `API_DATABASE_URL`, "news_api_login") still describes the
pre-5.6 model. Not to be "fixed" by an agent: the owner must pick one (requirement conflict).
- Verify: read `main.py:32`; compose has no `API_DATABASE_URL`.

### IA-003 · undocumented setting · P2 · app/core/config.py:33 vs .env.example
`API_DATABASE_URL` exists in Settings and is required in production, but is absent from
`.env.example` (CLAUDE.md: every setting documented there) and from compose. Depends on IA-002.

### IA-004 · UI/schema drift (attribution) · P2 · app/web/static/app.js:67 vs app/schemas/analysis.py:142-153
The card reads `item.source?.name || item.source_name`; `BriefingSummary` has neither, so every
list card says "Read at original article" without naming the publisher. The rule is "always
link out with attribution". Fix: add `source: BriefingSource` to `BriefingSummary` (the query
already joins `Source`) or drop the dead fallback.

### IA-005 · UI/schema drift · P2 · app/web/static/app.js:184,264 vs briefing.py:40-49
Unverified paths are mapped to panel keys by first segment. `$.overview.headline` → `overview`,
for which no panel exists, so the headline (rendered as the page `h1`) is never marked "Not
checked against the article", although it is the most prominent unverified field.

### IA-006 · dead fallbacks · P3 · app/web/static/app.js:263,275
`b.unverified_fields` (BriefingOut ignores that key, `extra="ignore"`) and `d.url`
(BriefingDetail uses `source_url`) can never be set. Harmless, but mask future renames.

### IA-007 · misleading UI copy · P3 · app/web/static/app.js:276-277
Banner says "N item(s) removed: their quotes could not be verified", but `removed_items`
includes `copied_items` (briefing.py:83). The banner also appears only when
`review_required`, so originality removals alone are never shown.

### IA-008 · multiple sources of truth · P2 · status sets and the view predicate
- `article_analyses.status` values are string literals in the model (`article_analysis.py:45`)
  and in migrations, while other tables use `check_in(..., Enum)` (`enums.py:50`).
- The predicate `status IN ('succeeded','needs_review') AND briefing IS NOT NULL` is written
  in the view migration (`9420376cc156:65`), the model's partial index
  (`article_analysis.py:62`), `de01c28240f2:29`, and `aa372fadc9eb:249`.
- `alembic check` does not compare CHECK text or views (stated in `61b8c5b98bf3:83`), so the
  BASELINE "No new operations" does not cover them. Recommend: one enum + a test that reads
  `pg_constraint`/`pg_views` in the test DB and compares with the model.

### IA-009 · duplicated config · P3 · app/core/config.py:54 vs app/analysis/llm.py:43,129
`GEMINI_BASE_URL` is a setting whose only legal value is the constant in `llm.py`
(`.env.example:53` says so). Either drop the setting or derive it from the constant.

### IA-010 · rule wording vs code · P2 · app/analysis/llm.py:241-256
CLAUDE.md allows retrying "503/500/502/504/timeouts". The client treats every
`httpx.TransportError` (DNS failure, connection refused, offline Mac) as busy and retries
with no attempt cap; each attempt is a ledger reservation counted toward the daily limit.
The docstring (llm.py:10) says "no network" is intended, so this is doc drift, not a bug,
but an offline Mac can burn up to the daily budget of reservations (pauses 10-60 s).
Owner decision needed to either document it or cap offline retries.

### IA-011 · error text may echo model output · P3 · app/analysis/llm.py:206
`str(ValidationError)[:300]` is put in the exception message; pydantic includes the
offending input value, which can be article-derived text. Check where this message is
logged/stored (CLAUDE.md: never log article bodies). The API path already avoids this
(`analysis.py:159`).

### IA-012 · Neon pooler not enforced outside CI · P2 · app/ingestion/runner.py, .env.example:21
The workflow refuses `-pooler` URLs (`ingest.yml:50`) but the code does not: a Mac run (or
`news analyze`, `cli.py:271` advisory lock) against a pooled URL would take session advisory
locks through PgBouncer transaction pooling, which do not hold. Recommend a readiness check
in code (`readiness.py`) that refuses a `-pooler` host, so the guard is in one place.

### IA-013 · restore-test on Neon free plan · P2 · app/ops/backup.py:167-170, architecture 5.6
`restore-test` runs `createdb`/`pg_restore` of a full dump on Neon. That needs CREATEDB for
`neondb_owner` (granted on Neon: unknown here, not tested) and temporarily doubles storage on
a free plan with a storage cap. A failed run that cannot `dropdb` leaves a second full copy
of article text on Neon. Verify the cleanup path (`drop_db` in a `finally`) and the plan
limit before relying on it.

### IA-014 · runtime tz data · P3 (unknown) · app/analysis/budget.py:40, Dockerfile:26
`ZoneInfo("America/Los_Angeles")` is evaluated at import, and `app.core.config` imports
`budget`, so the API imports it too. There is no `tzdata` dependency; it relies on the OS
zoneinfo. ubuntu-latest has it; whether `python:3.13-slim` has `/usr/share/zoneinfo` is
**unknown** (BASELINE only built the image, it did not run it). Verify with
`docker run --rm news-backend:local python -c "import app.main"`.

## Checked, no finding
- `uv sync --no-dev` is enough for runtime: third-party imports in `app/` and `alembic/env.py`
  are fastapi, starlette, pydantic(_settings), sqlalchemy, alembic, psycopg, httpx, defusedxml;
  all in `[project.dependencies]` (pyproject.toml:11-20).
- `ENVIRONMENT=production` in `ingest.yml:33` affects only the API (`main.py:32,40,63-65`,
  `session.py:77`); no ingestion code reads it. Harmless, but it gives a false impression
  that production checks guard the ingest.
- Workflow guard: requires `sslmode=require|verify-full`, refuses `-pooler`;
  `.env.example:23` sample matches. Backups forward `PGSSLMODE`/`PGCHANNELBINDING` via env,
  never argv (`backup.py:99,150-157`).
- Catalog routes vs schemas: `ArticleOut`/`FeedOut`/`SourceOut` built explicitly; no body
  field; keyset on `(published_at, id)`; `limit` bounded at 100.
- Parsers vs fixtures: `tests/ingestion/feeds.py` emits `<guid>…?p=N`, RFC 822 `pubDate`,
  and news-sitemap `news:` namespace exactly as `parsers.py:66-111` reads them.
- Gemini client: endpoint and model allowlisted (`llm.py:129-133`), 429 stops, non-transient
  errors stop, no billable options in the payload (model, messages, temperature, max_tokens).

## Counts
P0: 0 · P1: 2 (IA-001, IA-002) · P2: 7 (IA-003, 004, 005, 008, 010, 012, 013) ·
P3: 5 (IA-006, 007, 009, 011, 014). Total 14.
