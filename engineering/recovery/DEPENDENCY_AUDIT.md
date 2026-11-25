# Dependency audit (2026-11-25)

Read-only audit. Commands run: `uv lock --check`, `uv tree --depth 1 --outdated`,
`uv export --frozen --no-hashes --all-groups` + `uvx pip-audit --no-deps --disable-pip -r`,
greps over `app tests alembic scripts`.

## Summary of checks

- `uv lock --check`: "Resolved 60 packages" with no error: lock is consistent.
- `uv tree --depth 1 --outdated`: no direct dependency flagged as outdated.
- `pip-audit` (exported requirements, all groups): "No known vulnerabilities found"
  (the editable project itself is skipped, as expected).
- Ruff pre-commit rev `v0.17.6` (`.pre-commit-config.yaml:3`) equals locked ruff 0.17.6.

## Direct dependency usage

| Package | Locked | Evidence of use |
| --- | --- | --- |
| fastapi | 0.165.0 | 25 imports (`app/main.py`, `app/api/**`) |
| uvicorn[standard] | 0.54.0 | `Dockerfile` CMD; CLAUDE.md run command. Extras (uvloop, httptools, websockets, watchfiles) are runtime auto-picks / `--reload`, not imported |
| pydantic-settings | 2.16.3 | `app/core/config.py` |
| sqlalchemy[asyncio] | 2.1.9 | 105 imports; `[asyncio]` pulls greenlet, also needed by `[tool.coverage.run] concurrency = ["greenlet"]` |
| alembic | 1.20.1 | `alembic/`, readiness check |
| psycopg[binary] | 3.3.9 | 5 imports; driver `postgresql+psycopg://` (`app/core/config.py:87-94`) |
| httpx | 0.28.1 | 13 imports (fetcher, Gemini client, tests' MockTransport) |
| defusedxml | 0.7.1 | `app/ingestion/parsers.py` (2 imports) |
| pytest, pytest-asyncio, pytest-cov | dev | imports / `asyncio_mode`, `--cov` in `pyproject.toml` addopts |
| pytest-randomly | dev | plugin, auto-loaded (no import needed) |
| ruff, mypy | dev | CI `ci.yml:69-73`, pre-commit |
| types-defusedxml | dev | stubs for mypy strict |
| pre-commit | dev | `README.md:336` (`uv run pre-commit install`) |

No unused direct dependency found.

## Findings

DA-001 · transitive import · P2 · `app/main.py:9-11`, `app/core/middleware.py:4-5`,
`app/core/errors.py:16` · Code imports `starlette` directly; it is only a transitive dep
of fastapi (locked 1.10.0). · Evidence: grep above; `starlette` absent from
`pyproject.toml` dependencies. · Impact: a fastapi bump could move starlette's API under
us without a declared constraint. · Risk of change: low. · Action: either declare
`starlette` explicitly or import via `fastapi` re-exports where they exist (`fastapi.middleware.cors`,
`fastapi.exceptions`); ASGI types have no fastapi re-export, so declaring is simpler. ·
Verify: `uv lock --check`, mypy, tests.

DA-002 · transitive import · P3 · 10 app modules (e.g. `app/core/config.py:6`,
`app/analysis/schema.py:16`) · `pydantic` imported directly, only transitive via fastapi
and pydantic-settings. · Impact: minimal (both hard-require pydantic v2), but undeclared.
· Action: add `pydantic>=2,<3` to dependencies. · Verify: as DA-001.

DA-003 · tool drift · P2 · `.github/workflows/ci.yml:65` · CI runs `uvx pre-commit`
(unpinned, latest from PyPI) while dev group locks `pre-commit` 4.7.0. · Impact: CI
pre-commit version is not reproducible and differs from local. · Action: use
`uv run pre-commit run --all-files` in CI (already synced by `uv sync --locked`). ·
Verify: CI run.

DA-004 · deprecation · P2 · `pyproject.toml` filterwarnings (`ignore:Using \`httpx\` with
\`starlette.testclient\``) · Starlette 1.10 deprecates httpx inside `TestClient` in favour of
`httpx2` (baseline: 1 `StarletteDeprecationWarning`). Project code still needs httpx
(fetcher, Gemini client). · Impact: none now; a future starlette major may drop httpx
TestClient support and break the test suite. · Risk: adding httpx2 is a new dependency
(needs justification per CLAUDE.md). · Action: keep the filter; track the starlette
changelog; revisit when the warning becomes an error. The filter is narrow (message
match), so it does not hide other warnings. · Verify: `uv run pytest` warning count.

DA-005 · unpinned image · P2 · `docker-compose.yml:6` · `postgres:18-alpine` is a
floating tag, not digest-pinned, unlike the Dockerfile images. Dependabot `docker`
ecosystem (`.github/dependabot.yml`) watches the Dockerfile; compose files need the
separate `docker-compose` ecosystem. · Impact: local/CI test DB can change minor version
silently. · Action: pin by digest and add a `docker-compose` Dependabot entry, or accept
as deliberate. · Verify: `docker compose config`.

DA-006 · stale floors · P3 · `pyproject.toml` dependencies · Lower bounds far below the
lock (fastapi `>=0.115` vs 0.165.0; sqlalchemy `>=2.0` vs 2.1.9; ruff `>=0.6` vs 0.17.6;
mypy `>=1.11` vs 2.4.1; pytest-asyncio `>=0.24` vs 1.5.0). · Impact: none with
`--locked`; floors are untested claims. · Action: optionally raise floors to tested
versions. · Verify: `uv lock --check`.

DA-007 · metadata · P3 · `pyproject.toml` `description = "Enterprise-grade FastAPI
backend"` does not describe the project. · Action: reword. · Verify: n/a.

## Fine as is (no finding)

- Dockerfile: python 3.13-slim and uv 0.11 pinned by digest; `uv sync --locked --no-dev`;
  non-root; healthcheck uses stdlib only. Dependabot ignores Python minor/major bumps
  deliberately (matches `.python-version` 3.13).
- GitHub Actions pinned by SHA (`ci.yml:45,47`, `ingest.yml:36,38`); Dependabot covers
  `github-actions` and `uv`.
- pre-commit-hooks v6.0.0, gitleaks v8.30.1 (SHA noted); mypy/hygiene hooks run via
  `uv run`, so they use locked versions.

## Counts

P0: 0 · P1: 0 · P2: 4 (DA-001, DA-003, DA-004, DA-005) · P3: 3 (DA-002, DA-006, DA-007)
