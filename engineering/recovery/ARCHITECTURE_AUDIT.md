# Architecture audit (2026-11-25, baseline commit `3274239`)

Synthesis of the ten recovery reports in this folder. Specialist findings are referenced
by ID, not repeated. New IDs (AR-xxx) are used only for cross-cutting issues. Owner
decisions in `docs/architecture.md` 5.4, 5.5 and 5.6 are requirements. Read-only: no
`.env`, no Neon, no pytest.

## 1. Module boundaries (verdict: sound)

| Boundary | Where | State |
| --- | --- | --- |
| Rights gate | `app/ingestion/policy.py` (the only route to storage) | Sound; covered by tests (TI report, "Critical invariants"). A silent downgrade is CA-004. |
| Analysis output vs API output | `app/analysis/schema.py` vs `app/schemas/analysis.py` | The split is right (it is the quote and rights boundary, CA-007), but the **contract between them is not tested** (AR-002). |
| API read path | `app/api/v1/*` reads `articles`, `feeds`, `sources`, the `analysis_briefings` view; engine is read-only | Sound. Article text cannot leave through a route. The briefing routes lack a sentinel test (TI-003). |
| Gemini quota | `budget.py` / `ledger.py` / `llm.py` | Sound. The open question is wording versus code on network errors (IA-010, TI-006). |
| Ingestion | `walk` / `store` / `runner` / `readiness` | Sound. The pooler guard is in the wrong layer (AR-003, IA-012). |
| Ops | `app/ops/backup.py` | Sound code. Restore-test placement on Neon (PA-001, IA-013) is an owner question. |

Dependency direction is clean: `api -> schemas/db/models`, `analysis -> models/db`,
`ingestion -> models/db`, `cli -> everything`. One wrinkle: `app.core.config` imports
`app.analysis.budget` (for the ceilings), so the API imports the budget module and its
timezone lookup at import time (IA-014). This is acceptable. Do not refactor it unless
IA-014 shows a real failure.

No overengineering was found in the domain code (CA report, "Overengineering"). The one
piece of machinery that has lost its purpose is AR-001.

## 2. Cross-cutting findings

**AR-001 · architecture vs owner decision · P1** · `app/main.py:32-34`,
`app/db/session.py:77` (`assert_least_privilege`), `app/core/config.py:33`
(`api_database_url`), `scripts/db_roles.sql`, `scripts/db_login_passwords.sql`,
`tests/integration/test_database_security.py`, CLAUDE.md "Roles" and "Startup privilege
guard", architecture 5.3/6.
- The least-privilege design (NOLOGIN groups, two login roles, `API_DATABASE_URL`, a
  production refusal) was built for the local-first model. Decision 5.6 says one
  `DATABASE_URL` everywhere and that the logins "are not used". The code still makes a
  production API impossible under that decision (IA-002, SA-003, DOC-001), the setting is
  undocumented (IA-003), and `ENVIRONMENT=production` in `ingest.yml` suggests a
  protection that does not exist (SA-004).
- **Merges:** IA-002, SA-003, DOC-001, IA-003, SA-004.
- **What protects article text today** (this does not depend on the roles): the
  read-only engine (`create_engine(read_only=True)`) and the fact that no route reads
  `article_bodies`. Tests cover both (TI report).
- **Options:** see RECOVERY_ROADMAP "Owner decisions", D1 and D7. This is not changed
  without the owner.

**AR-002 · contract drift between pipeline, view, schema and UI · P1** ·
`app/analysis/briefing.py:139-142`, `app/schemas/analysis.py:17-24,75-77`,
`app/web/static/app.js`, migrations `9420376cc156`, `aa372fadc9eb`.
- There are four representations of one briefing: the builder output, the frozen JSON
  in the migration, `BriefingOut`, and the JS reader. Nothing checks that they agree.
  The confirmed defect is IA-001: real data had `main_event` null in 6 of 10 briefings,
  and those briefings were dropped by the API. The same class of drift appears in IA-004
  to IA-007 (UI fields that never arrive). The status predicate is copied four times and
  is invisible to `alembic check` (IA-008).
- **Root cause:** no round-trip contract test of "builder output -> stored ->
  view -> `BriefingOut` -> UI keys".
- **Merges:** none. It groups IA-001, IA-004, IA-005, IA-006, IA-007, IA-008 and CA-007
  for batching.

**AR-003 · safety guards live in YAML and test config, not in code · P2**
- The Neon-pooler refusal exists only in `ingest.yml` (IA-012).
- The "never test on Neon" guard is a host denylist in `tests/conftest.py` with no test
  of its own (TI-001).
- The workflow guard is tested by substring match (TI-005).
- CI and local test environments diverge in ways that broke `main` four times (BC-002).
- **Principle for the fixes:** put a guard in code once, test it as a pure function, and
  have YAML call or mirror it.
- **Groups:** IA-012, TI-001, TI-005, BC-002.

**AR-004 · sources of truth for limits and settings · P2**
- The Gemini limits appear in four places (DOC-009).
- `GEMINI_BASE_URL` is a setting with one legal value (IA-009).
- `CONCURRENCY_CEILING` is a dead alias (CA-001).
- **Target:** numbers live in code (`CEILINGS`) and in architecture 5.5. CLAUDE.md
  states them once, because it is the agent rulebook and the owner wants them visible.
  README and `.env.example` link or mirror them. Do not change any value.
- **Groups:** DOC-009, IA-009, CA-001.

**AR-005 · Neon storage is unmeasured · P2**
- PA-001, PA-002, PA-003 and IA-013 all depend on numbers nobody has read: table sizes,
  and the plan cap (the owner says 1 GB; PA-003 says it is unverified).
- **Order:** measure first, with read-only `pg_total_relation_size` queries run by the
  owner. Then decide. Retention is an owner decision (D4).
- **Groups:** PA-001, PA-002, PA-003, IA-013.

## 3. Sources of truth (target)

| Fact | Single source | Mirrors allowed |
| --- | --- | --- |
| Gemini ceilings | `app/analysis/budget.py` `CEILINGS` | architecture 5.5 and CLAUDE.md (stated once), `.env.example` defaults |
| Settings names and defaults | `app/core/config.py` | `.env.example` (complete, including `API_DATABASE_URL` if it is kept) |
| Briefing shape | `app/analysis/briefing.py` | `BriefingOut` and `app.js`, enforced by a contract test (AR-002) |
| Analysis status set | one Python enum | migrations, partial index, view (a test compares them, IA-008) |
| Connection model | architecture 5.6 | CLAUDE.md, README (after D1) |
| Doc ownership | DOCUMENTATION_AUDIT "Target structure" | accepted as is |

## 4. Build/CI and integration (by reference)

- **CI correctness:** BC-001 (gitleaks scans nothing; verify the hook entry first),
  BC-002 (environment leakage), DA-003 (unpinned `uvx pre-commit`), BC-008 (two ruff
  versions).
- **Operability of decision 5.6:** BC-003. Schedule disablement is already documented in
  5.6 ("re-enable it in the Actions tab"). Only failure notification remains, and it
  needs `issues: write`, so it is an owner choice (D8).
- **Reproducibility:** BC-004 and DA-005 are duplicates. BC-006, BC-007, BC-010 are
  hygiene items.
- **Runtime image:** IA-014 needs one `docker run` to settle.

## 5. Rejected or refuted at architecture level

- TI-008: refuted. CLAUDE.md no longer contains the stale phrases (lead verification,
  grep empty).
- Weakening or removing any guard, roles script or test silently: not allowed. AR-001
  goes to the owner.
