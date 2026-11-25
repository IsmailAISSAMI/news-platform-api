# Codebase audit: dead code, duplication, abstraction (2026-11-25)

Scope: `app/`, `scripts/`, `alembic/`. Tests used only as usage evidence. Read-only run;
pytest not run (baseline 734 passing per BASELINE.md).

Method: `uvx vulture app scripts --min-confidence 60` (~80 hits), then `grep -rnw` over
`app scripts alembic tests` and `docs/` for each candidate.

## Vulture triage

Nearly all hits are framework false positives and are **not** findings:
- FastAPI route handlers (`app/api/health.py:15,21`, `app/api/v1/catalog.py:45-146`,
  `app/api/v1/analysis.py:71,92`): registered by decorator.
- Pydantic `model_config`, `@field_validator`/`@model_validator` methods
  (`app/core/config.py:76,90,98`, `app/analysis/schema.py:83,110`) and schema fields
  (`app/analysis/schema.py`, `app/schemas/*.py`): read by pydantic / serialised.
- SQLAlchemy mapped columns (`app/models/*`), `Base.metadata` (`app/db/base.py:17`).
- `HTMLParser` hooks (`app/ingestion/normalize.py:26-38`), `logging.Formatter.format`
  (`app/core/logging.py:12`).
- `IngestionRun` attribute assignments (`app/ingestion/runner.py:245-255`): persisted.
- `app/ops/backup.py:49,53` dataclass fields: reported output.

## Findings

| ID | Category | P | Location | Summary |
|----|----------|---|----------|---------|
| CA-001 | dead code | P3 | app/analysis/budget.py:55 | `CONCURRENCY_CEILING` alias never referenced |
| CA-002 | test-only API | P3 | app/analysis/budget.py:220 | `RequestBudget.window_usage()` used only by tests |
| CA-003 | test-only field | P3 | app/ingestion/walk.py:70,84 | `items_unchanged` counted but never reported |
| CA-004 | test-only field | P3 | app/ingestion/policy.py:96 | `GateDecision.downgrade_reason` never read in production |
| CA-005 | deferred feature | P3 | app/models/discovered_url.py, app/models/enums.py:30-37 | Archive backlog schema with no writer/reader |
| CA-006 | duplication (justified) | P3 | app/cli.py:301 + app/analysis/lab.py:58 | Lab ceiling checked twice |
| CA-007 | duplication (justified) | P3 | app/analysis/schema.py vs app/schemas/analysis.py | Parallel analysis models |

No P0-P2 dead code, unreachable branches or abandoned modules found. `count_today`
(vulture hit at `budget.py:115`, `ledger.py:73`) is a `Ledger` Protocol method with a real
implementation used by `reserve` logic and integration tests: not dead.

## Details

**CA-001** · dead code · P3 · `app/analysis/budget.py:55`
- Evidence: `grep -rnw CONCURRENCY_CEILING app scripts alembic tests` -> only the
  definition. Concurrency ceiling is enforced via `CEILINGS["concurrency"]` directly.
  Contrast `LAB_PER_RUN_CEILING` (used in `app/cli.py:30,301`, `app/analysis/lab.py:19,58`).
- Impact: none; minor noise. Risk of removal: very low (constant, not a ceiling value;
  `CEILINGS` itself is unchanged, so CLAUDE.md's limit rules are untouched).
- Action: delete the alias, or use it where `CEILINGS["concurrency"]` is read.
- Verify: ruff, mypy, `pytest -m "not integration"`.

**CA-002** · used only by tests · P3 · `app/analysis/budget.py:220`
- Evidence: `grep -rnw window_usage` -> `tests/analysis/test_budget.py:172,191,216` only.
- Impact: small introspection helper kept for test assertions. Risk: removing forces tests
  to read `_window`. Action: keep (cheap, read-only); optionally expose it in
  `news gemini-usage` so it has a production caller.

**CA-003** · used only by tests · P3 · `app/ingestion/walk.py:70,84`
- Evidence: `grep -rnw items_unchanged` -> walk.py:70,84 and
  `tests/integration/test_walk.py:296`; no runner report / `ingestion_runs` column uses it.
- Impact: the run report (CLAUDE.md "prints a report") omits an already-computed number.
- Action: add it to the printed report (preferred) rather than delete. Risk: low.

**CA-004** · used only by tests · P3 · `app/ingestion/policy.py:96,139`
- Evidence: `grep -rnw downgrade_reason` -> policy.py + `tests/ingestion/test_policy.py:152,160,177`.
  `walk.py:320` calls `apply_policy` but never reads/logs the reason.
- Impact: a rights downgrade (e.g. expired licence) happens silently in production logs.
- Action: log `downgrade_reason` at WARNING in `walk.py` when non-None. Do not change the
  gate itself (owner rule). Risk: low; logging only, no article text.

**CA-005** · deferred feature · P3 · `app/models/discovered_url.py`, `app/models/enums.py:30` (`DiscoveryChannel.ARCHIVE`), `:35,37` (`BacklogStatus.RESOLVED/GONE`)
- Evidence: `grep -rn "DiscoveredUrl\|BacklogStatus" app scripts` -> only models and
  `models/__init__.py`; no ingestion code writes `discovered_urls`. `docs/architecture.md:10`
  "Deferred: the archive backlog (milestone 6)". `IngestionRun.backlog_resolved/remaining`
  (`app/models/ingestion_run.py:37-38`) likewise unused.
- Impact: schema carries an unused table/columns. This is a documented deferral, not
  abandonment. Action: none; keep (removing needs a migration and would be undone in M6).
  Note in docs that the table is intentionally empty.

**CA-006** · duplication (defence in depth) · P3 · `app/cli.py:301-302`, `app/analysis/lab.py:58-59`
- Evidence: both compare `budget.per_run` with `LAB_PER_RUN_CEILING`; the CLI comment says
  "settings already refuse it; never trust". Three layers (settings, CLI, lab).
- Action: keep. It guards an owner-mandated limit; simplifying it is not worth the risk.

**CA-007** · parallel models (justified) · P3 · `app/analysis/schema.py` (LLM output,
strict, with `Evidence` quotes) vs `app/schemas/analysis.py` (API response)
- Evidence: same leaf names (`area`, `certainty`, `tone`, `topics`...) in both
  (vulture lines schema.py:157-196, schemas/analysis.py:70-121). The API layer adds
  `source_url`, `removed_items`, `unverified_fields` and does not mirror evidence quotes.
- Impact: two definitions of shared `Literal` vocabularies can drift.
- Action: keep the split (it is the rights boundary: article quotes must not be served).
  Optionally import shared `Literal` aliases from one module. Risk: low; verify with
  API tests + `alembic check` unaffected.

## Overengineering

No needless abstraction found. `Ledger` Protocol (`budget.py:112`) has two implementations
(`DatabaseLedger`, `tests/analysis/fake_ledger.py`), so it earns its keep.

## Counts

P0: 0 · P1: 0 · P2: 0 · P3: 7
