# Baseline (2026-11-25, before any recovery change)

Recorded on the owner's Mac, branch `main`, commit `3274239`, clean working tree. Nothing
was modified to obtain these results.

## Passing checks

| Check | Command | Result |
| --- | --- | --- |
| Locked install | `uv sync --locked` | OK |
| Lint | `uv run ruff check .` | OK |
| Format | `uv run ruff format --check .` | OK |
| Types (strict) | `uv run mypy app tests alembic` | OK, 121 files |
| Models vs migrations | `uv run alembic check` (against Neon) | No new operations |
| Repo hygiene | `uv run python scripts/check_repo_hygiene.py` | OK |
| Full tests | `uv run pytest` (local test DB, `TEST_DATABASE_URL`) | 734 passed, 0 skipped, 23 s |
| Branch coverage | (pytest-cov, `fail_under = 95`) | 95.83 % |
| Unit tests only | `uv run pytest -m "not integration"` | 576 passed, 158 deselected |
| pre-commit | `uvx pre-commit run --all-files` | all hooks pass |
| Production image | `docker build .` | OK |
| GitHub CI | run #18 on `3274239` | success |

## Failing checks

None.

## Skipped checks and warnings

- No skipped tests in a normal local run.
- 1 warning: `StarletteDeprecationWarning` (FastAPI's `TestClient` imports
  `starlette.testclient` with `httpx`; the warning recommends `httpx2`). Third-party, not
  raised by project code.
- The backup round-trip tests skip when no `pg_dump` can reach the test database (for
  example `PG_TOOLS_COMMAND=""` on a Mac without host PostgreSQL tools). CI sets
  `REQUIRE_BACKUP_TESTS=1`, so there they must run.

## Environmental notes

- The repository sits in an iCloud-synced folder. Untracked conflict copies exist
  locally (`.venv 2`, `.coverage 2`, `.coverage 3`, `.venv-icloud-corrupted.nosync`);
  they are not in git and are not project defects.
- Tests require the local Docker `db` container and `TEST_DATABASE_URL`; they refuse a
  Neon URL by design.
- The application database is Neon (`DATABASE_URL`); `alembic check` and the API were
  verified against it on 2026-11-25.

## Commands that must not run during the recovery

- `news ingest` (hits the real publisher; only on the owner's request).
- `news analyze` / `news lab` (Gemini API, real quota; never from the recovery).
- `alembic upgrade`/`downgrade` against Neon, and any write to Neon.
- Reading `.env` or printing secrets; deleting Docker volumes or databases.
- `git commit` / `git push` without the owner's explicit approval.

## Size at baseline

151 tracked files; `app/` 7,777 lines of Python; `tests/` 8,391 lines; docs (README,
CLAUDE.md, `docs/*.md`) about 1,700 lines.
