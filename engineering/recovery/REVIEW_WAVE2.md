# Review wave 2 (2026-11-25)

Checks re-run by the reviewer on its own DB (news_rev2 -> news_rev2_test, dropped after):
ruff check: All checks passed; ruff format --check: 158 files already formatted;
mypy app tests alembic: no issues in 126 files; check_repo_hygiene.py: OK;
pytest (full): 790 passed, coverage 95.94%. Note: a later targeted re-run of the llm and
briefing tests (20 passed) was run without exporting TEST_DATABASE_URL, so it used the
default local test DB from config, not news_rev2. Local Docker only, no Neon.

## 1. B9 `app/api/v1/analysis.py` semi-join: APPROVE
- The LATERAL `latest` filters `view.article_id == Article.id` and is joined with
  `true()` (an inner join), so an article with no view row is already dropped. The added
  `Article.id IN (SELECT article_id FROM analysis_briefings)` keeps exactly the same set.
  The LATERAL subquery is unchanged, so the choice of latest briefing (updated_at DESC
  LIMIT 1) is the same. ORDER BY and the cursor predicate are unchanged, so order and pages
  are the same.
- A NULL briefing cannot matter: the view excludes `briefing IS NULL` and only
  succeeded/needs_review rows (alembic 9420376cc156, view definition). A NULL article_id
  could never satisfy either predicate. IN, not NOT IN, so no NULL trap.
- Detail route: the same query plus `Article.id == :id`, so the same result.
- `BriefingSummary.source` (wave 1 note) is kept: `_summary(row.Article, row.Source, ...)`.
- Measurements are taken from the batch report. I did not re-measure them.

## 2. B-D2 `app/analysis/llm.py`: APPROVE
- `_is_network_error`: ConnectTimeout and PoolTimeout return True, other
  TimeoutException subclasses return False, and every other TransportError returns True.
  This matches the wave 1 finding.
- The count resets only when `response is not None`, which means an HTTP answer. A read
  timeout sets response=None, so it does not reset the count.
- It cannot add requests. The cap only makes the loop stop sooner. `_give_up_offline` records the
  current reservation through `_finish(..., "failed")` before it raises
  LLMUnavailableError, and each attempt is still reserved before it is sent. The ledger
  is not bypassed. Read and write timeouts stay uncapped, as owner decision 5.5 says.

## 3. Docs: REQUEST CHANGES (one item)
Rights text (5.4), the geography rule and the legal disclaimer are not in the diff.
5.5 and 5.6 are amended with dated notes ("Amended 2026-11-25 (owner decision D2)",
"Note (2026-11-25, owner decision D1)", "Amended ... D3"), and nothing was deleted.
The CLAUDE.md rule bullets are the same before and after (`diff` of bullet lines: only the
Neon/pooler line was extended).

Blocking: CLAUDE.md goes beyond D9. D9 (RECOVERY_ROADMAP.md line 173) allows "only
the role allowlist text ... Keep the limit numbers in CLAUDE.md, because they are the agent
rule." The limit numbers are kept. But the batch also removed from the hard rules:
"per-minute and token limits are at least one third below Google's; the daily limit is
stricter", the enforcement paragraph (CEILINGS constants for lab 3 and concurrency 8, the
per-minute window shared via `api_calls` and seeded from the last 60 s, tokens estimated
as UTF-8 bytes // 3 (+1)), and the guard's trigger (superuser or can SELECT
article_bodies; refuse in production, warn elsewhere). All of this text still exists in
architecture 5.5 and section 6 (lines ~321-328, ~489-495), so nothing is lost from the
repo. It is still a CLAUDE.md quota/security rule removed without owner approval.
Fix: restore those sentences in CLAUDE.md, or get the owner's explicit approval for the
wider dedup.

Spot-checks against code (27):
1. MAX_TRANSPORT_ERRORS = 5 (llm.py) ✓
2. ConnectTimeout/PoolTimeout counted (llm.py `_is_network_error`) ✓
3. Reset only on HTTP answer ✓
4. MAX_ATTEMPTS = 3 per article (pipeline.py:74) ✓
5. 20/min, 10,000 tok/min, 1,000/day (budget.py:47-51) ✓
6. Retry pauses 10, 20, 30, 60 (llm.py:54) ✓
7. Production API refuses without API_DATABASE_URL (main.py:32-34) ✓
8. assert_least_privilege is called from the lifespan (main.py:36), defined in session.py:50 ✓
9. Superuser or article_bodies refuses in prod and warns elsewhere (session.py:77-79) ✓
10. is_production is used only in main.py and session.py (API), never by ingest ✓
11. Pooler refused by ingest (cli.py:234), analyze (_check_ready, 389), backup (510) via
    wait_for_database -> refuse_pooled_host (readiness.py:46) ✓
12. Scratch DB `<db>_restore_check` (backup.py:201) ✓
13. Hint `docker compose up -d db` when the local db is unreachable (backup.py:326) ✓
14. 14 backups kept (config.py:72) ✓
15. 7-day reminder (config.py:73, cli.py:254) ✓
16. Exit codes 0/1/2/3/130 (cli.py:75 and docstring) ✓
17. compose and CI use the same postgres digest (docker-compose.yml:7, ci.yml:24) ✓
18. CI full-tree gitleaks, sha256-checked, `dir . --redact` (ci.yml:67-76) ✓
19. CI `uv run pre-commit run --all-files` (ci.yml:81) ✓
20. CI unit run with PG_TOOLS_COMMAND="" and REQUIRE_BACKUP_TESTS=1 (ci.yml:40-42) ✓
21. fail_under = 95 (pyproject.toml:101) ✓
22. MIN_COPIED_RUN = 8 (briefing.py:37) ✓
23. OverviewOut/WhatOut/ToneOut fields nullable (schemas/analysis.py) ✓
24. BriefingSummary carries `source` (analysis.py `_summary`) ✓
25. Run report shows "N unchanged" (README sample, tests/test_cli.py) ✓
26. GEMINI_BASE_URL defaults to the constant, any other value refused (.env.example).
    I did not re-read config.py for this one; marked unknown.
27. ingest.yml keeps `environment: production` (GitHub environment for the secret), so
    the `.env.example` wording "environment `production`" is still true ✓
Not verified: the "about 0.5% of articles" and timing claims (no data access).
Extra limit numbers: the same limit numbers in CLAUDE.md, 5.5 and budget.py are what D9 allows.

## 4. `ENVIRONMENT: production` removed from ingest.yml: APPROVE
`grep is_production app`: only app/main.py:32,40,63-65 and app/db/session.py:77
(assert_least_privilege, which is only called from main.py:36). The `news ingest` path
never reads it, so ingest behavior is unchanged. Keeping it would have had no effect on
ingest today, and it would make an API started with that env refuse to start (D1).

## Overall: REQUEST CHANGES, limited to the CLAUDE.md D9 scope in item 3. Items 1, 2 and 4 approved.

## Resolution of REQUEST CHANGES (item 3, CLAUDE.md beyond D9), 2026-11-25

Restored by the lead in CLAUDE.md, verbatim in substance: "at least one third below
Google's; the daily limit is stricter", the "How the limits are enforced" paragraph
(`CEILINGS`, the shared per-minute window seeded from the last 60 s of `api_calls`,
the UTF-8 bytes // 3 (+1) estimate), and the privilege-guard trigger (superuser or can
SELECT `article_bodies` → refused in production, warned elsewhere). Only the role
allowlist text remains moved to architecture section 6, as D9 allows. Item 3: resolved.
