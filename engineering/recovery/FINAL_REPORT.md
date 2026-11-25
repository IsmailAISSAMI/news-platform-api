# Final engineering report (2026-11-25)

Recovery of `news-platform-api` from baseline commit `3274239`. All changes are
uncommitted until the owner approves. Details per finding are in the audit reports,
`RECOVERY_ROADMAP.md`, `REVIEW_WAVE1.md` and `REVIEW_WAVE2.md` in this folder.

## 1. Initial condition

All checks were green at baseline (`BASELINE.md`): 734 tests, 95.83 % branch coverage,
ruff, mypy strict, `alembic check`, hygiene, pre-commit, Docker build, CI. The audits
confirmed the suspicion of AI-layered drift only partly: **no dead modules, no needless
abstractions in the domain code, and no test disabled or loosened to hide a bug** (every
removed assertion since d401ad6 was traced to a stricter check or a documented change).
The real problems were a few correctness bugs that the suite did not catch, docs and
code disagreeing, and guards that lived only in YAML or test configuration.

## 2. Architecture findings

Boundaries hold: rights gate, read-only API path, Gemini budget, checkpoint walk
(`ARCHITECTURE_AUDIT.md`). Cross-cutting issues: unused least-privilege machinery vs the
one-URL decision (AR-001, settled by D1/D7), no contract between the briefing builder,
the API schema and the UI (AR-002), safety guards that existed only in YAML or test
config (AR-003), and Neon storage never measured (AR-005).

## 3. Critical problems found and fixed

| ID | Problem | Fix | Proof |
| --- | --- | --- | --- |
| IA-001 (P1) | A briefing whose copied headline/main event/tone was nulled by the originality filter failed the API schema and **silently disappeared** (list skip, detail 404). | Those fields are `str \| None`; UI handles null. | Contract test through the real builder: 7/8 failed before, 8/8 after. Integration test on both routes. |
| IA-010 + review (P2) | Offline Mac retried Gemini **forever**, reserving up to the whole daily budget of 1,000. | Network errors (incl. connect/pool timeouts) give up after 5 in a row (D2); only an answer resets the count. | 8 new tests, failing first. Ceilings unchanged. |
| New (packaging) | The Docker image lacked `app/web/static`: **the API container could not start**. | `[tool.setuptools.package-data]`. | `docker run … import app.main` OK; 5 UI files present. |
| PA-001/IA-013 (P1) | `restore-test` restored a full copy on Neon (doubles storage, second copy of article text). | Restores into the local Docker `db` (D3). | Real run: Neon dump read-only, verified locally, scratch DB dropped. |
| PA-004 (P1) | Briefings list scanned most articles when few are analysed. | One redundant semi-join. | 1 % briefed: 8,083→2,205 buffers, 47→2 ms; oldest-only case 116→1.2 ms. |
| BC-001 (P1) | gitleaks in CI scanned nothing (staged-only hook). | Full-tree gitleaks step, SHA-256 verified. | Upstream hook entry quoted; checksum re-verified by reviewer. |
| BC-002 (P1) | CI-only env broke `main` four times. | Autouse fixture isolates unit tests from CI variables. | `PG_TOOLS_COMMAND="" REQUIRE_BACKUP_TESTS=1 pytest -m "not integration"`: 622 passed. |
| TI-001 (P1) | The Neon guard for tests had no test. | 11 tests. | Mutating the guard makes 4 fail. |
| IA-011, IA-012 | Error text could echo model output; only the workflow refused Neon's pooler. | Errors carry locations/types only; readiness refuses `-pooler` hosts. | Sentinel tests; pure-function tests. |

## 4. Dead code removed

`CONCURRENCY_CEILING` alias (unused); dead UI fallbacks (`b.unverified_fields`,
`d.url`, `item.source_name`); the fake ledger's dead `"pending"` filter. Everything else
flagged by vulture was a false positive; the archive-backlog schema stays (deferred
milestone 6, documented as such).

## 5. Dependencies

Nothing removed (every dependency is used). `starlette` and `pydantic` declared
explicitly (were transitive), no version change in `uv.lock`. pre-commit in CI now runs
from the lock (`uv run`), one ruff version. `pip-audit`: no known vulnerabilities.

## 6. Tests corrected or added

734 → 790 tests, coverage 95.83 % → 95.94 %. New: briefing contract, conftest guards,
ledger contract (fake vs real), schema invariants (CHECK, view, partial index),
readiness, D2 retry cap, local restore, rights-downgrade logging, UI attribution.
Changed expectations, each reviewed: report line gains "unchanged"; one misleading test
renamed (assertions unchanged); workflow tests made stricter. No assertion weakened.

## 7. Documentation

README, CLAUDE.md, architecture, analysis and decision records now describe the current
system (Neon, one `DATABASE_URL`, daily GitHub ingest, Gemini; LM Studio marked
historical with dated notes). Owner decisions amended with dated notes, never deleted;
rights and geography text untouched. CLAUDE.md: only duplicated role text moved to
architecture section 6 (D9); quota and guard rules kept (restored after review).

## 8. Comments

Stale `pyproject.toml` comment and "Enterprise-grade" description removed; module
docstrings that made false claims corrected (`analysis.py` probe count, `llm.py` retry
policy). No broad comment sweep: the audit found no misleading comment worth the risk.

## 9. Refactoring

Kept minimal by design: `_is_network_error` and `_error_summary` helpers (`llm.py`),
`restore_target`/`is_remote` (`backup.py`), `refuse_pooled_host` (`readiness.py`),
one source of truth for `GEMINI_BASE_URL`. Complexity limits (C901 ≤ 12) hold.

## 10. Security

See §3 plus: `.claude/settings.json` now asks before `alembic upgrade` and denies more
`.env` variants (D5); ingest workflow no longer sets a misleading `ENVIRONMENT`; rights
downgrades are logged (no article text). Git history: no secrets, no article text.

## 11. Performance

Only PA-004, measured before and after (above). Retention not applied (D4: measure first).

## 12. Build and CI

Full-tree gitleaks; pre-commit from the lock replaces duplicate lint/type steps; compose
Postgres pinned to the CI digest; iCloud conflict copies ignored in git and Docker
context; UI files shipped in the image.

## 13. Validation, before → after

| Check | Baseline | Final |
| --- | --- | --- |
| `uv sync --locked`, `uv lock --check` | OK | OK |
| ruff check / format | OK | OK |
| mypy strict | OK (121 files) | OK (126 files) |
| `alembic check` | clean | clean |
| hygiene, pre-commit | OK | OK |
| pytest | 734 passed, 0 skipped | 790 passed, 0 skipped |
| coverage | 95.83 % | 95.94 % |
| unit tests under CI env | not checked | 622 passed |
| Docker build / image import | build OK, **import failed** | OK / OK |
| Neon backup + restore-test | verified (restored on Neon) | verified (restored locally) |
| Independent review | — | wave 1 and 2 approved after 2 fixes |

Warnings: one third-party `StarletteDeprecationWarning` (unchanged, DA-004 kept).
GitHub CI on the new code is **not verified yet**: it runs only after the owner approves
a commit and push.

## 14. Remaining technical debt

- PA-004 residual: if the newest ~16k articles all lack briefings, the list still walks
  them (~10 ms at 20k articles); acceptable, revisit at scale.
- `FakeLedger.totals()` not covered by the ledger contract (pending tracking differs).
- Restricted-login machinery kept as optional (D7); only needed for a production API.
- The archive backlog (milestone 6) remains deferred.
- DA-006: minimum versions in `pyproject.toml` far below the locked ones.

## 15. Known risks

- **SA-001 (P0, owner action): confirm the Neon owner password was rotated** after it
  appeared in a screenshot, then update `.env` and the `DATABASE_URL` GitHub secret.
- One owner URL in GitHub (accepted in 5.6): a leaked secret is full access. Cheap
  mitigations: restrict the `production` environment to `main`; enable secret scanning
  and push protection.
- GitHub disables scheduled workflows after 60 days without repository activity.
- Neon free plan terms can change.

## 16. Maintenance recommendations

Measure Neon table sizes every few months (D4) and decide retention at 50 % of the cap;
run `news backup && news restore-test` weekly (`docker compose up -d db` first); push
through a branch or PR so CI runs before `main`; keep the "test fails first" rule.

## Change inventory

| Path | Reason |
| --- | --- |
| `app/schemas/analysis.py`, `app/web/static/app.js`, `app/api/v1/analysis.py` | IA-001 nullable copied fields; IA-004..007 attribution/UI; PA-004 query |
| `app/analysis/llm.py` | D2 retry cap (+ review fix), IA-011 error text |
| `app/ingestion/readiness.py` | IA-012 pooler refusal |
| `app/ops/backup.py` | D3 local restore-test |
| `app/ingestion/walk.py`, `app/cli.py` | CA-004 downgrade log; CA-003 "unchanged" in report |
| `app/core/config.py`, `app/analysis/budget.py` | IA-009 single source; CA-001 dead alias |
| `pyproject.toml`, `uv.lock` | DA-001/002 declared deps; package data (image fix); BC-009 |
| `.github/workflows/ci.yml`, `.pre-commit-config.yaml` | BC-001 gitleaks; DA-003/BC-008 one toolchain |
| `.github/workflows/ingest.yml` | SA-004 misleading `ENVIRONMENT` removed |
| `docker-compose.yml`, `.gitignore`, `.dockerignore` | BC-004 pinned image; BC-006/007 iCloud copies |
| `.claude/settings.json` | D5 permissions |
| `.claude/agents/*.md` | The 12 recovery agents |
| `README.md`, `CLAUDE.md`, `docs/**`, `.env.example` | B8, D1, D2, D3, D8, D9 documentation |
| `tests/**` (7 modified, 5 new) | Section 6 |
| `engineering/recovery/*.md` | Audits, roadmap, reviews, this report |
