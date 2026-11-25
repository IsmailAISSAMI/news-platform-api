# Recovery roadmap (2026-11-25, baseline `3274239`)

Sources: the ten audit reports in this folder and ARCHITECTURE_AUDIT.md (AR-xxx).
Priorities follow the specialist reports unless a change is noted. Owner decisions in
architecture 5.4, 5.5 and 5.6 are requirements.

## Standard verification ("STD")

Every batch runs these:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy app tests alembic
docker compose up -d db
TEST_DATABASE_URL=<local test DB URL on :5433> uv run alembic check
                    # against the local test DB, never Neon
TEST_DATABASE_URL=<...> uv run pytest   # full suite, local test DB; coverage >= 95 %
uv run python scripts/check_repo_hygiene.py
```

- Add `docker build .` when the batch touches `Dockerfile`, dependencies or `app/` imports.
- **Rollback for every batch:** revert only that batch's exclusive files. Batches do not
  touch migrations, so no schema rollback is needed.
- Nothing is committed without the owner's approval.

## a. Master findings table

Status values: **A** = accepted (batch number), **R** = rejected, **D** = needs owner
decision (question number), **M** = merged into another finding.

| ID | P | One line | Status |
| --- | --- | --- | --- |
| AR-001 | P1 | Least-privilege machinery conflicts with 5.6's one URL | D1, D7 |
| AR-002 | P1 | No contract test from briefing builder to view, `BriefingOut` and UI | A: B1, B4d |
| AR-003 | P2 | Safety guards live in YAML and conftest, not in tested code | A: B2, B4a, B4d |
| AR-004 | P2 | Limits and settings have several sources of truth | A: B6, B8 |
| AR-005 | P2 | Neon storage is unmeasured | D4 |
| CA-001 | P3 | `CONCURRENCY_CEILING` alias is dead | A: B6 |
| CA-002 | P3 | `window_usage()` is used only by tests | R: cheap and read-only; keep |
| CA-003 | P3 | `items_unchanged` is computed but not reported | A: B6 (add to the run report) |
| CA-004 | P3 | Rights downgrade reason is never logged | A: B6 (WARNING log only; the gate is unchanged) |
| CA-005 | P3 | `discovered_urls` has no writer (milestone 6 deferred) | R: documented deferral; doc note M → DOC-007 |
| CA-006 | P3 | Lab ceiling is checked twice | R: defence in depth for an owner limit |
| CA-007 | P3 | Parallel analysis and API models | R: the split is the rights boundary; drift is covered by AR-002 |
| TI-001 | P1 | Conftest Neon guard and `_test` rewrite are untested | A: B4a (tests only; allowlist idea → R, guard not changed) |
| TI-002 | P2 | `FakeLedger` filters differ from the real ledger | A: B4c |
| TI-003 | P2 | No body-sentinel test on the briefing routes | A: B4b |
| TI-004 | P3 | Test name over-promises | A: B4b |
| TI-005 | P2 | Workflow guard is tested by substring | A: B4d |
| TI-006 | P2 | Tests pin retry on connection errors beyond CLAUDE.md wording | D2 (M → IA-010) |
| TI-007 | P3 | Startup guard tests are mocks only | R: real check covered in integration; noted |
| TI-008 | P2 | CLAUDE.md stale versus 5.6 | R: refuted (phrases no longer in CLAUDE.md) |
| DOC-001 | P1 | Docs say one URL; code refuses a production API without `API_DATABASE_URL` | M → AR-001 (D1) |
| DOC-002 | P2 | README says "no scheduler" | A: B8 |
| DOC-003 | P2 | `decisions/security.md` says the DB is never exposed | A: B8 |
| DOC-004 | P2 | Architecture status line and section 2 read as current | A: B8 |
| DOC-005 | P2 | `model-selection.md` still says "adopted" | A: B8 |
| DOC-006 | P2 | `analysis.md` LM Studio design reads as current | A: B8 |
| DOC-007 | P3 | `backlog.py` path does not exist | A: B8 (mark as planned; add CA-005 note) |
| DOC-008 | P3 | "Mac not always online" framing | A: B8 (architecture only; CLAUDE.md M → D9) |
| DOC-009 | P2 | Facts duplicated across README, CLAUDE.md and architecture | A: B8 for README and architecture; CLAUDE.md part D9 |
| DOC-010 | P3 | CLAUDE.md length | D9 |
| DA-001 | P2 | `starlette` imported but not declared | A: B5 |
| DA-002 | P3 | `pydantic` imported but not declared | A: B5 |
| DA-003 | P2 | CI uses unpinned `uvx pre-commit` | A: B7 |
| DA-004 | P2 | httpx TestClient deprecation | R: narrow filter is fine; a new dependency is not justified |
| DA-005 | P2 | Compose postgres tag not pinned | M → BC-004 |
| DA-006 | P3 | Stale version floors | R: harmless under `--locked`; churn |
| DA-007 | P3 | "Enterprise-grade" description | M → BC-009 |
| SA-001 | P0 | Neon owner password appeared in a screenshot | D6 (owner action) |
| SA-002 | P1 | One URL gives full access if leaked; GitHub-side mitigations | D6 (GitHub settings, outside the repo) |
| SA-003 | P1 | Production API needs `API_DATABASE_URL`; 5.6 does not say so | M → AR-001 (D1) |
| SA-004 | P3 | `ENVIRONMENT=production` in `ingest.yml` has no effect | M → AR-001 (D1) |
| SA-005 | P2 | Ingest traceback may show host and user in public logs | R: host and user are not secrets; the secret is masked; a catch-all hides diagnostics |
| PA-001 | P1 | Restore-test doubles storage on Neon | D3 |
| PA-002 | P2 | `raw_output` and JSON copies kept forever | D4 |
| PA-003 | P2 | No retention; plan cap unverified | D4 (measurement by the owner, read-only) |
| PA-004 | P1 | `/briefings` LATERAL probes many articles when few are analysed | A: B9 (measure locally first) |
| PA-005 | P2 | Rare filter plus ordered index | R: negligible at current size; re-measure later |
| PA-006 | P3 | `api_calls` index choice | R: tiny; no measured cost |
| PA-007 | P2 | Pool settings on Neon | R for now: needs Neon-side measurement; revisit with D4 data |
| PA-008 | P3 | Ingest request count | R: no issue found |
| PA-009 | P3 | Token throughput | R: no change (limits are owner rule) |
| BC-001 | P1 | gitleaks scans nothing in CI (plausible) | A: B7 (verify the hook entry first) |
| BC-002 | P1 | Job environment leaks into unit tests; CI differs from local | A: B4a |
| BC-003 | P1 | Scheduled ingest can be disabled or fail silently | D8 (re-enabling is already documented in 5.6) |
| BC-004 | P2 | Compose postgres not digest-pinned | A: B7 |
| BC-005 | P2 | `alembic upgrade` auto-allowed in `.claude/settings.json` | D5 |
| BC-006 | P2 | `.venv 2` not ignored | A: B7 |
| BC-007 | P3 | `.dockerignore` misses venvs and docs | A: B7 |
| BC-008 | P3 | ruff and mypy run twice; two ruff versions | A: B7 (with DA-003) |
| BC-009 | P3 | Stale description and comment in `pyproject.toml` | A: B6b |
| BC-010 | P3 | No Dependabot for pre-commit | R: BC-008 removes the main drift; manual autoupdate is enough |
| IA-001 | P1 | Copied scalars set to null fail `BriefingOut`; briefings are dropped (confirmed) | A: B1 |
| IA-002 | P1 | Production API impossible under 5.6 | M → AR-001 (D1) |
| IA-003 | P2 | `API_DATABASE_URL` missing from `.env.example` | M → AR-001 (blocked on D1) |
| IA-004 | P2 | List cards show no publisher name (attribution rule) | A: B3 |
| IA-005 | P2 | Unverified headline never marked | A: B3 |
| IA-006 | P3 | Dead JS fallbacks | A: B3 |
| IA-007 | P3 | Misleading "removed" banner | A: B3 |
| IA-008 | P2 | Status predicate copied four times; `alembic check` is blind to it | A: B4d (test only; no enum or DDL refactor, which would need a migration) |
| IA-009 | P3 | `GEMINI_BASE_URL` setting duplicates a constant | A: B6 (derive the default from the constant; the allowlist is unchanged) |
| IA-010 | P2 | Network errors retried without a cap; burns daily reservations | D2 |
| IA-011 | P3→P2 | Validation error text may contain article-derived text | A: B2 (raised: article-text logging rule) |
| IA-012 | P2 | Pooler URL not refused in code | A: B2 |
| IA-013 | P2 | Restore-test on Neon: CREATEDB and storage | M → PA-001 (D3) |
| IA-014 | P3 | tzdata in slim image unknown | A: B7 (verify by `docker run`; add `tzdata` only if it fails) |

## b. Owner decisions required

**D1. Production API vs one URL (AR-001, IA-002, SA-003, DOC-001, IA-003, SA-004).**
Today `ENVIRONMENT=production` makes the API refuse the owner URL.
- (a) Keep the production refusal and document that a production API needs
  `API_DATABASE_URL` = `news_api_login`.
- (b) Make one URL true everywhere: the production check warns instead of refusing.
  The read-only engine stays.
- (c) Do nothing.
- **Recommendation: (a).** It is the simplest option consistent with both 5.6 and the
  rule "never weaken the guard". No production API exists today, so one sentence in
  5.6, `.env.example` and the README costs nothing. Also drop `ENVIRONMENT: production`
  from `ingest.yml` (SA-004), or comment that it has no effect.

**D2. Retrying network errors (IA-010, TI-006).**
- (a) Document the current behavior in CLAUDE.md and 5.5: transport errors count as
  temporary, bounded by the daily limit.
- (b) Cap retries on transport errors (for example, stop after 3 consecutive
  `TransportError`s).
- **Recommendation: (b).** An offline Mac should stop, not reserve up to 1,000 calls.
  It is a small change in `llm.py` plus updated tests. Also state the "unusable answer
  retried up to 3 times" rule in CLAUDE.md.

**D3. Where `restore-test` restores (PA-001, IA-013).**
- (a) Keep Neon.
- (b) Restore into the local Docker container.
- **Recommendation: (b).** No Neon storage spike, no CREATEDB question, no risk of a
  leftover copy of article text. It needs an edit to 5.6's backup bullet.

**D4. Storage retention (PA-002, PA-003, AR-005).**
- First, the owner runs the read-only size queries in PA-002 and PA-003 and reads the
  plan cap in the Neon console.
- Options:
  - (a) No retention yet; take a monthly size snapshot.
  - (b) Null `raw_output` on `succeeded` rows older than N days, and delete rows of
    superseded `analyzer_version`s.
- **Recommendation:** (a) now. Choose (b) only if the projection crosses 50 % of the
  cap. This is the cheapest way to protect the 1 GB.

**D5. `.claude/settings.json` (BC-005).** Move `Bash(uv run alembic upgrade:*)` to
`ask`, and add `Read(./.env.*)` to deny. **Recommendation:** yes. `DATABASE_URL` is
production.

**D6. Credentials (SA-001, SA-002).** Confirm that the Neon owner password was rotated
after the screenshot, that `.env` and the `production` secret were updated, and that a
`workflow_dispatch` run succeeded. Optional: restrict the `production` environment to
`main`, and turn on secret scanning and push protection. **Recommendation:** rotate now,
if not done already. This blocks nothing in the code batches.

**D7. Unused restricted-login machinery under 5.6 (AR-001).** Today this is the roles
scripts, the two login roles, `API_DATABASE_URL`, `assert_least_privilege` and the
security tests.
- (a) Keep it as the documented optional path, which is what 5.6 says now.
- (b) Remove the login roles and `API_DATABASE_URL`, but keep the read-only engine and
  the `article_bodies` grant test.
- **Recommendation: (a).** It is already built, tested and cheap to keep. Removal needs
  a migration-free but wide change plus an edit to CLAUDE.md rules, for little gain. If
  D1 = (b), revisit.

**D8. Ingest failure notification (BC-003).**
- (a) Rely on GitHub's email to the cron editor.
- (b) Add a failure step that opens an issue, which needs `issues: write`.
- **Recommendation: (a).** Add a one-line README note to check the Actions tab weekly.

**D9. CLAUDE.md dedup and length (DOC-009 part, DOC-008 part, DOC-010).** May the
documentation engineer replace duplicated role and limit detail in CLAUDE.md with links
to the architecture doc? **Recommendation:** only the role allowlist text (after D1/D7).
Keep the limit numbers in CLAUDE.md, because they are the agent rule.

## c. Batches (decision-free first)

Order: correctness/security → test integrity → dependency → refactor → comments → docs
→ build/CI → measured performance. Files are exclusive within a batch. Batches that
share a file run in the order listed. "Review" means `independent-verifier` must approve
the batch before the next batch touches the same files.

**B1 · correctness · IA-001 (AR-002) · integration-specialist · Review: yes**
- **Files:** `app/schemas/analysis.py`, `tests/analysis/test_briefing_contract.py` (new).
- **Change:** make `OverviewOut.headline/report_type/event_status`,
  `WhatOut.main_event` and `ToneOut.label/explanation` `str | None`. Add a test that
  builds a briefing with a copied headline, main_event and tone explanation, then
  validates it through `BriefingOut` and `BriefingDetail`.
- **Out of scope:** the builder, the migrations, and the frozen migration copy (never
  edit migrations).
- **Check first:** `app.js` already falls back on null (IA-001). Confirm by reading.
- **Risk:** low. The API schema gets wider (null allowed); no data change.
- **Verify:** STD. The new test fails on baseline code.

**B2 · security/correctness · IA-011, IA-012 (AR-003) · security-engineer · Review: yes**
- **Files:** `app/analysis/llm.py` (only the line that builds the `ValidationError`
  message), `app/ingestion/readiness.py`, `tests/analysis/test_llm.py`,
  `tests/ingestion/test_readiness.py` (or the existing readiness test file).
- **Change, part 1:** build the error message from error locations and types only
  (`e.errors(include_input=False)`). Test with a sentinel string that it never appears.
- **Change, part 2:** readiness refuses a database host containing `-pooler` before the
  lock. Test it as a pure function. `ingest.yml` keeps its own guard.
- **Risk:** low. Note that `news analyze` also takes the lock, so it is also refused on
  a pooler URL. That is intended.
- **Verify:** STD.
- **Depends on:** must not overlap B-D2 in `llm.py`. Run B2 first.

**B3 · attribution/UI · IA-004, IA-005, IA-006, IA-007 · integration-specialist · Review: yes**
- **Files:** `app/schemas/analysis.py` (after B1), `app/api/v1/analysis.py`,
  `app/web/static/app.js`, `tests/integration/test_api_briefings.py`.
- **Change:**
  - Add `source: BriefingSource` to `BriefingSummary` and fill it from the existing
    `Source` join.
  - Map `$.overview.*` unverified paths to the headline.
  - Remove the dead `b.unverified_fields` and `d.url` fallbacks.
  - Banner text: "N item(s) removed (unverified quotes or copied wording)", shown
    whenever `removed_items > 0`.
- **Risk:** low. This is additive in the API.
- **Verify:** STD, plus a manual `/ui` check on the local test DB with synthetic data
  only.

**B4a · test integrity · TI-001, BC-002 · test-integrity-engineer · Review: yes (touches the conftest guard)**
- **Files:** `tests/conftest.py` (add a fixture only), `tests/test_conftest_guards.py`
  (new).
- **Change:**
  - Unit-test `_local_only` (a Neon host is refused for both URLs) and
    `_test_database_url` (`news` → `news_test`; `news_test` stays the same).
  - Add an autouse fixture that unsets `PG_TOOLS_COMMAND` and `REQUIRE_BACKUP_TESTS`
    for tests not marked `integration`.
  - Do not change the guard logic.
- **Risk:** low.
- **Verify:** STD, plus `PG_TOOLS_COMMAND="" REQUIRE_BACKUP_TESTS=1 uv run pytest -m "not integration"`.
  Mutation check: temporarily make `_local_only` return its input; the new tests fail.
  Revert the mutation.

**B4b · test integrity · TI-003, TI-004 · test-integrity-engineer · Review: no**
- **Files:** `tests/integration/test_api.py`, `tests/integration/test_api_briefings.py`
  (after B3).
- **Change:** a body-sentinel test on the briefing list and detail routes, and rename
  `test_no_response_ever_carries_a_body`.
- **Verify:** STD.

**B4c · test integrity · TI-002 · test-integrity-engineer · Review: no**
- **Files:** `tests/analysis/fake_ledger.py`, `tests/integration/test_ledger_contract.py`
  (new).
- **Change:** the fake filters on the imported `SENT_OUTCOMES`. Add a contract test
  that runs the same scenarios against both ledgers.
- **Verify:** STD.

**B4d · test integrity · IA-008, TI-005 (AR-002, AR-003) · test-integrity-engineer · Review: no**
- **Files:** `tests/integration/test_schema_invariants.py` (new),
  `tests/test_workflows.py`.
- **Change, part 1:** compare the `pg_constraint` CHECK text for
  `article_analyses.status`, the `pg_views` definition of `analysis_briefings`, and the
  partial-index predicate against one expected set in the test.
- **Change, part 2:** assert that the pooler and sslmode guard lines sit inside the
  guard step's `run:` block (stdlib slicing), not just anywhere in the file.
- **Verify:** STD.

**B5 · dependency · DA-001, DA-002 · dependency-specialist · Review: no**
- **Files:** `pyproject.toml` (dependencies only), `uv.lock`.
- **Change:** `uv add "starlette>=1.10,<2" "pydantic>=2,<3"`. The ranges must contain
  the locked versions; do not upgrade them.
- **Verify:** `uv lock --check`, STD, `docker build .`.

**B6 · refactor · CA-001, CA-003, CA-004, IA-009 (AR-004) · refactoring-specialist · Review: yes (budget and gate files)**
- **Files:** `app/analysis/budget.py` (delete the alias only), `app/ingestion/walk.py`,
  `app/ingestion/runner.py` (report line), `app/core/config.py` and `.env.example`
  (`GEMINI_BASE_URL` default taken from the `llm.py` constant), related tests.
- **Rules:**
  - `CEILINGS` values, the allowlist and the gate logic stay the same.
  - The CA-004 log carries the reason only, never article text.
- **Verify:** STD, plus `grep -n CEILINGS app/analysis/budget.py` (values unchanged).

**B6b · comments/metadata · BC-009 (DA-007) · comment-cleanup-specialist · Review: no**
- **Files:** `pyproject.toml` (the description and the stale line-74 comment only; run
  after B5).
- **Verify:** STD.

**B8 · docs · DOC-002 to DOC-008, DOC-009 (README and architecture part), CA-005 note · documentation-engineer · Review: yes (architecture doc)**
- **Files:** `README.md`, `docs/architecture.md` (status line, section 2 title, 10
  roadmap path, 12 note; **not** the decision text in 5.4, 5.5 or 5.6),
  `docs/decisions/security.md`, `docs/decisions/model-selection.md`,
  `docs/analysis.md`.
- **Rules:** follow the target structure in DOCUMENTATION_AUDIT. Dated notes, no
  rewriting of history. Geography rule applies.
- **Verify:** link check by grep, STD (hygiene).

**B7 · build/CI · BC-001, DA-003 + BC-008, BC-004, BC-006, BC-007, IA-014 · build-ci-specialist · Review: yes (CI)**
- **Step 1:** read the upstream gitleaks `.pre-commit-hooks.yaml` at the pinned SHA.
  Proceed only if its entry is staged-only. If so, add a CI step running a SHA-pinned
  gitleaks over the full tree. Otherwise mark BC-001 as rejected.
- **Step 2:** CI runs `uv run pre-commit run --all-files`. Drop the duplicate ruff step,
  or switch the ruff hook to `language: system` with `uv run ruff`, so one version runs.
- **Step 3:** pin the compose postgres image to the same digest as `ci.yml`.
- **Step 4:** add iCloud conflict patterns (`* [0-9]`, `* [0-9].*`, `*.nosync`) to
  `.gitignore` and `.dockerignore`, plus `engineering/`, `docs/` and `.claude/` to
  `.dockerignore`.
- **Step 5 (IA-014):** run `docker build -t news-backend:local . && docker run --rm news-backend:local python -c "import app.main"`.
  Only if that fails, add `tzdata`, justified.
- **Files:** `.github/workflows/ci.yml`, `.pre-commit-config.yaml`,
  `docker-compose.yml`, `.gitignore`, `.dockerignore`, `tests/test_workflows.py` (if it
  asserts the CI steps; after B4d).
- **Verify:** STD, `docker compose config`, `docker build .`,
  `uv run pre-commit run --all-files`, `git status --short --ignored`. CI green on push
  after the owner approves the commit.

**B9 · measured performance · PA-004 · performance-engineer · Review: yes**
- **Measure first** on the local test DB, with synthetic articles only: 20k articles at
  1 %, 10 % and 100 % briefed. Run `EXPLAIN (ANALYZE, BUFFERS)` on page 1 and on a deep
  cursor page.
- Change `app/api/v1/analysis.py` only if the buffers show the probe blow-up. Drive the
  query from the partial briefing index, keep keyset order and the response the same.
- **Files:** `app/api/v1/analysis.py` (after B3), a benchmark script in the scratchpad
  (not committed).
- **Verify:** STD, plus before and after EXPLAIN numbers recorded in the batch report.

### Blocked batches

| Batch | Blocked on | Files | Content |
| --- | --- | --- | --- |
| B-D1 | D1 (and D7) | `docs/architecture.md` 5.6, `.env.example`, `README.md`, `.github/workflows/ingest.yml` (`ENVIRONMENT` line); CLAUDE.md only with owner approval | Option (a): document `API_DATABASE_URL` for a production API; fix SA-004. Run after B8. |
| B-D2 | D2 | `app/analysis/llm.py`, `tests/analysis/test_llm.py`, CLAUDE.md/5.5 wording (owner-approved) | Cap consecutive transport-error retries, or document them. Run after B2. Review: yes. |
| B-D3 | D3 | `app/ops/backup.py`, `tests/integration/test_backup_roundtrip.py`, architecture 5.6 backup bullet | Restore into local Docker. Review: yes. |
| B-D4 | D4 | none until the owner shares measurements | Retention only if approved; it would need a migration or a CLI command. |
| B-D5 | D5 | `.claude/settings.json` | Move upgrade to `ask`; deny `.env.*`. |
| B-D8 | D8 | `README.md` (after B8) or `ingest.yml` | Note, or a failure-issue step. |
| B-D9 | D9 | `CLAUDE.md` | Dedup of roles text. |

## d. Will not do

- Reverse or "fix" owner decisions 5.4 (private full text), 5.5 (Gemini free tier) or
  5.6 (Neon, one URL, daily GitHub ingest).
- Raise any Gemini ceiling, widen the model allowlist, add billable options, retry more
  than the rules allow, or call the real API from tests.
- Read `.env`, connect to or write to Neon, run `alembic upgrade`/`downgrade` against
  Neon, or run `news ingest`, `analyze` or `lab`.
- Weaken the `tests/conftest.py` Neon guard, `assert_least_privilege` or the read-only
  API engine. Delete the roles scripts or security tests without D7.
- Edit an existing migration (including the frozen briefing copy in `aa372fadc9eb`), or
  refactor the status CHECKs or view into an enum (that needs a migration; IA-008 is
  test-only).
- Remove the archive-backlog schema (CA-005) or the lab double check (CA-006).
- Add a dependency without measured need (`httpx2`, `tzdata` unless IA-014 fails).
- Delete Docker volumes or databases, commit or push without approval, or use real
  article text in fixtures.
- Apply retention or data deletion on Neon.
