# Build / CI audit (2026-11-25, commit 3274239, read-only)

Scope: pyproject tool sections, pre-commit, Dockerfile, .dockerignore, docker-compose,
ci.yml, ingest.yml, dependabot.yml, check_repo_hygiene.py, .claude/settings.json, .gitignore.
Verified: every `news` subcommand in CLAUDE.md answers `--help` (ingest, backup,
restore-test, analyze, lab, gemini-usage, lab-compare, eval export, eval score).
CI steps = local definition of done (ruff check+format, mypy app tests alembic, alembic
upgrade+check, full pytest, hygiene) plus pre-commit. `uv sync --locked` in CI, ingest and
Dockerfile. Actions pinned by SHA; Dockerfile images pinned by digest.

## Findings

**BC-001** · CI/secrets · **P1** · .pre-commit-config.yaml:17-20, ci.yml:64-65
Gitleaks is a no-op in CI. The upstream `gitleaks` hook runs `gitleaks git --pre-commit
--staged`; in CI nothing is staged, so `pre-commit run --all-files` scans nothing. The step
name claims "gitleaks" coverage. Impact: a secret committed with `--no-verify` or via the
web UI passes CI (hygiene script is the only net, and it uses regexes). Risk of change: low.
Action: add a CI step `gitleaks dir .` (or `gitleaks git` on the pushed range) via a pinned
binary/action, keep the staged hook locally. Verify: plant a fake key on a branch, CI fails.

**BC-002** · CI regression guard · **P1** · ci.yml:37-42; runs #14-#17
Runs #14, #15, #16 failed at collection: `RuntimeError: REQUIRE_BACKUP_TESTS=1 but pg tools
cannot reach DATABASE_URL` (tests/integration/test_backup_roundtrip.py). Fixed in 3d9357f
(probe after the test DB exists). #17 then failed 2 unit tests in tests/test_db_session.py
because the job-level `PG_TOOLS_COMMAND: ""` leaked into unit tests that assumed the local
default; fixed in 3274239 by pinning the variable per test. #18 green.
Recurrence: nothing structural prevents it. Job-level env applies to all tests; the local
run (docker-exec pg tools, no REQUIRE_BACKUP_TESTS) differs from CI, and pre-commit runs no
tests, so four broken pushes reached `main`. Action: (a) autouse fixture in tests/conftest.py
that clears PG_TOOLS_COMMAND/REQUIRE_BACKUP_TESTS for non-integration tests;
(b) document a "CI-like" local invocation (`REQUIRE_BACKUP_TESTS=1 uv run pytest`);
(c) optionally run CI on a branch/PR before pushing to main. Verify: run unit tests with
`PG_TOOLS_COMMAND=""` exported locally.

**BC-003** · ingest ops · **P1** · .github/workflows/ingest.yml:9-14
Scheduled workflows in a public repo are disabled by GitHub after 60 days without repo
activity; the daily ingest would stop silently. No failure notification besides GitHub's
default email to the actor (for cron: the last editor of the cron line). Action: add a
keepalive (e.g. monthly no-op commit is undesirable; prefer re-enabling via
`gh workflow enable` reminder or a keepalive action pinned by SHA), and a failure step
(e.g. open an issue with `gh issue create`, needs `issues: write`). Owner decision 5.6 is
not questioned, only its operability. Verify: `gh workflow view ingest.yml` state.

**BC-004** · reproducibility · **P2** · docker-compose.yml:6
Local test DB `postgres:18-alpine` is unpinned, while CI uses a digest (ci.yml:24) and
Dependabot `docker` only scans Dockerfile. Local and CI can drift. Action: pin the same
digest in compose (add `directory: /` compose entry is already covered only if Dependabot
supports compose; else bump by hand alongside ci.yml).

**BC-005** · tool safety · **P2** · .claude/settings.json:10
`Bash(uv run alembic upgrade:*)` is auto-allowed although DATABASE_URL is Neon (production)
and BASELINE.md forbids upgrades against Neon during recovery. `downgrade` is in `ask`, but
`upgrade` is not. Action: move `alembic upgrade` to `ask`. Also `.env` deny list misses
`.env.*` variants other than local/production (pattern `Read(./.env.*)`).

**BC-006** · hygiene · **P2** · .gitignore:9-13
`.venv 2` (present on disk) is not ignored: `.venv/` and `.venv` match exact names only.
`.coverage 2/3` are covered by `.coverage*`. A `git add -A` would stage the whole duplicate
venv (check-added-large-files may catch some). Action: add `* [0-9]` / `* [0-9].*` style
iCloud-conflict patterns, or at least `.venv */`. Verify: `git status --short --ignored`.

**BC-007** · docker context · **P3** · .dockerignore
Misses `*.nosync` (`.venv.nosync`, the real venv), `.venv 2`, `.coverage *`, `engineering/`,
`docs/`, `.claude/`. Only `app/` and `alembic*` are copied, so the image is unaffected, but the
build context uploads the venv(s). Action: mirror the .gitignore patterns.

**BC-008** · pre-commit/CI duplication · **P3** · ci.yml:64-73
ruff and mypy run twice (pre-commit hooks then dedicated steps). ruff-pre-commit v0.17.6 can
also differ from the locked ruff in uv.lock (two ruff versions). Action: either drop the
explicit steps or make the ruff hook `language: system` with `uv run ruff`, so one version.

**BC-009** · metadata drift · **P3** · pyproject.toml:8, 74
Description "Enterprise-grade FastAPI backend" is stale; comment at line 74 references a
per-file ignore that no longer exists. Dev dep floors (`ruff>=0.6`, `mypy>=1.11`) are loose
but harmless under `--locked`.

**BC-010** · dependabot · **P3** · dependabot.yml:15-19, run list 2026-11-22
Python image minor bumps are ignored (good, after the failed 3.15 PR). Note uv image
`ghcr.io/astral-sh/uv:0.11@sha256` is pinned and Dependabot docker will bump it; fine.
No `pre-commit` ecosystem entry: hook revs (ruff, pre-commit-hooks, gitleaks) are never
updated automatically. Action: `pre-commit autoupdate` periodically, or rely on BC-008.

No issue: Dockerfile is multi-stage, non-root (uid 1000), digest-pinned, healthcheck, no
tests/.env copied. CI permissions `contents: read`, concurrency and timeouts set on both
workflows; ingest validates the URL (direct endpoint, sslmode) and has no dev deps.

## Counts
P0: 0 · P1: 3 · P2: 3 · P3: 4 (total 10)
