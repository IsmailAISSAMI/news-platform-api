# Test integrity audit (2026-11-25, baseline commit 3274239)

Scope: `tests/` (734 tests, 95.83 % branch coverage per BASELINE.md). Read-only audit.
Only unit file run: `uv run pytest -m "not integration" --no-cov -q -p no:randomly
tests/analysis/test_llm.py` -> `44 passed in 0.16s`. No integration or full-suite run.

## Summary

No test was found disabled, `xfail`ed or loosened to hide a bug. The only `pytest.skip`
(tests/integration/test_backup_roundtrip.py:67) is guarded by `REQUIRE_BACKUP_TESTS=1`
(fails instead in CI). No bare `pytest.raises(Exception)` exists (grep of
`pytest.raises\((Exception|RuntimeError|ValueError)\)` returns nothing). Every assertion
removed in the audited commits was either replaced by a stricter one or follows a
documented behavior change. Gaps are mostly missing tests on guards, and fakes that drift
from the real ledger.

## Review of loosened / removed assertions (git history)

| Commit | Change | Verdict |
| --- | --- | --- |
| 3f254f7 | tests/analysis/test_briefing.py: "out-of-range paths are harmless" -> now `pytest.raises(UnresolvablePathError)`; malformed paths now raise | Legitimate, **stricter** (fail closed instead of serving an item). |
| 3f254f7 | test_api_briefings.py: `source_url == url` -> `"url" not in body` plus new `copied_items`, `unverified_fields` checks | Legitimate (duplicate field removed), stricter. |
| 3f254f7 | test_api_briefings.py:246: `review["removed_items"] == 1` -> `== 0` | Legitimate: verification now runs on the cleaned analysis (commit message; architecture section 7); the event date is nulled by cleanup, and the test still asserts `review["when"]["event_date"] == {"value": None}`. |
| 3f254f7 | test_database_security.py: per-table `has_privilege` asserts rewritten | Rewritten as loops over `news_api`/`news_api_login` x INSERT/UPDATE/DELETE/TRUNCATE; coverage widened, not narrowed. |
| 3f254f7 | test_evidence.py soft-hyphen assert rewritten | Legitimate rewrite (same rule, new form). |
| d401ad6 | test_budget.py: daily-limit / history / 0o600 file-mode asserts removed | Legitimate: the local usage file was replaced by the `api_calls` ledger (architecture 5.5); daily limit now tested in tests/integration/test_ledger.py:49,152 and test_llm.py:316; `test_no_local_file_is_involved_in_the_budget` added. |
| d401ad6 | test_llm.py: `used_today()` counts removed | Moved to ledger `totals()` assertions (test_llm.py:178,293,322). Legitimate. |
| b609e95 / 198bb6c | fixed backoff `[60,120,240]` and "4 attempts" overload error removed | Legitimate per CLAUDE.md: temporary overloads are retried with the same model until answered; bounded by the daily limit (test_llm.py:316). |
| ddc02db | LM Studio tests removed | Legitimate (LM Studio retired, owner decision 2026-11-24). |
| 8b5ea33 | test_workflows.py: `secrets.INGEST_DATABASE_URL` -> `secrets.DATABASE_URL` | Follows owner decision 5.6 (one connection string). A requirement, not a finding; noted in TI-008. |
| 3d9357f / 3274239 | backup probe moved to a fixture; PG_TOOLS_COMMAND pinned | Legitimate; makes tests deterministic. |

## Findings

**TI-001** · missing test · P1 · tests/conftest.py:28-58 · The Neon guard (`_local_only`)
and the `_test` rewrite (`_test_database_url`) have no direct test. `grep -rn
"_local_only\|_test_database_url" tests` matches only conftest itself. A regression
(e.g. host check broken) would silently let the suite create/migrate a database on Neon.
Also the guard only matches `host.endswith(".neon.tech")`; any other remote host passes.
Impact: data integrity of the production DB. Risk of change: low (pure functions).
Action: unit-test both functions (Neon host refused for TEST_DATABASE_URL and
DATABASE_URL; `news` -> `news_test`; `news_test` unchanged; API_DATABASE_URL rewritten or
dropped); consider an allowlist of local hosts instead of a Neon denylist (owner call).
Verify: new tests fail when `_local_only` returns the URL unchanged.

**TI-002** · fake realism · P2 · tests/analysis/fake_ledger.py:21-27,63-68 vs
app/analysis/ledger.py:40,54,142 · `FakeLedger.count_today` filters `outcome != "pending"`,
a value `reserve` never writes (dead condition), and `recent()` includes every outcome,
while `DatabaseLedger` filters `SENT_OUTCOMES = ("answered","refused_busy","failed")` in
both. Today no code records `no_send` (grep finds it only in the ledger docstring), so
results agree, but the fake would count `no_send` rows the real ledger ignores. The fake
also has no lock, so unit tests cannot detect reservation races (covered only by
tests/integration/test_ledger.py:49). Action: filter on the imported `SENT_OUTCOMES` in
the fake; add a contract test running the same scenarios against both ledgers.

**TI-003** · missing test · P2 · tests/integration/test_api_briefings.py · Briefing
endpoints are checked for leaked quotes/evidence (lines 185-187, 216, 247) but no test
stores an `ArticleBody` with a sentinel and asserts it is absent from `/api/v1/briefings`
and the detail route, as tests/integration/test_api.py:203-217 does for the catalog.
Action: extend `test_stored_bodies_are_never_served` to the briefing routes.

**TI-004** · name over-promises · P3 · tests/integration/test_api.py:198
`test_no_response_ever_carries_a_body` checks only key names on one endpoint (list). The
real guarantee comes from the next test (sentinel). Action: rename (e.g.
`test_list_items_have_no_body_keys`) or extend to every route.

**TI-005** · substring-only assertions · P2 · tests/test_workflows.py:38-42 ·
`test_ingest_refuses_the_neon_pooler_and_plain_text_connections` asserts only that the
strings `*-pooler.*` and `sslmode=require` occur somewhere in ingest.yml; it would pass if
they appeared in a comment or the check were inverted. Same style throughout the file
(acknowledged "no YAML dep"). Action: parse the guard step (stdlib text slicing of the
step) or test the guard script itself with sample URLs.

**TI-006** · spec ambiguity in tested behavior · P2 · tests/analysis/test_llm.py:297
(`"offline"` -> `httpx.ConnectError` retried) and :356 · CLAUDE.md allows retrying only
"503/500/502/504/timeouts"; a connection error is not listed, yet the tests pin it as
retried. Not a loosened test, but the tests encode behavior beyond the written rule.
Also architecture 5.5 (docs/architecture.md:337) says unusable answers are retried up to 3
times per article, which CLAUDE.md does not list. Action: owner to confirm; then align
CLAUDE.md or the code. Do not change tests before that decision.

**TI-007** · mock-only coverage · P3 · tests/test_app.py:85-109 · Startup guard tests
monkeypatch `assert_least_privilege`; they test the lifespan wiring only. The real check
is covered in tests/integration/test_database_security.py:136-143, so acceptable; noted
so nobody counts test_app as guard coverage.

**TI-008** · doc conflict (not a test defect) · P2 · CLAUDE.md ("local PostgreSQL",
"no GitHub-hosted ... runner") vs docs/architecture.md 5.6 (Neon, daily GitHub ingest)
and tests/test_workflows.py, which enforce 5.6. Architecture line 33 says 5.6 wins. Tests
follow the owner decision; CLAUDE.md is stale. Action: update CLAUDE.md (owner approval).

## Critical invariants: coverage check

- Rights gate: tests/ingestion/test_policy.py (17 tests) + test_policy_matrix.py:32 cover
  every policy, missing basis, expired/missing license; walk tests check bodies only in
  `article_bodies` (test_walk.py:259,448). Adequate.
- Gemini ceilings: test_budget.py:96,113,225 (settings can only lower; CEILINGS values);
  daily limit test_llm.py:316 and test_ledger.py:49,152; no real API (conftest dummy key
  assertion, conftest.py:85-86). Adequate.
- Checkpoint walk: test_walk.py:134,162,214,487,518,559,578,592 (interrupted run, date
  floor, page limit, rewalk never moves checkpoint, republished checkpoint). Adequate.
- API never exposes text: catalog yes (test_api.py:203); briefings partial (TI-003);
  `news_api` cannot read `article_bodies`: test_database_security.py.
- Conftest Neon guard: untested (TI-001).
- Ingest workflow: string checks only (TI-005).

## Counts by priority

P0: 0 · P1: 1 (TI-001) · P2: 5 (TI-002, TI-003, TI-005, TI-006, TI-008) · P3: 2 (TI-004, TI-007)
