# Security audit (2026-11-25)

Scope: secrets, DB privileges, API exposure of `article_bodies`, workflows, Docker,
subprocess, dependencies. Read-only audit; `.env` not read, Neon not contacted, no tests run.
Owner decisions 5.4/5.5/5.6 are treated as requirements.

## Checks that passed (evidence)

- **Git history, 34 commits:** `git log --all -p | grep -E "npg_…|scheme://user:pass@|AIza…|sk-…"`
  matched only placeholders (`change-me`, `test-only-password`, `ci-password`). No real
  secret found. A grep for real article text was not exhaustive; `scripts/check_repo_hygiene.py`
  and gitleaks run in CI (`.github/workflows/ci.yml`).
- **Actions:** both workflows `permissions: contents: read`; actions pinned by SHA
  (`checkout@3d3c42e…`, `setup-uv@1db35dd…`); CI service image pinned by digest; ingest
  has no `pull_request` trigger; Dependabot covers uv, actions, docker (`.github/dependabot.yml`).
- **Docker:** runtime runs as `appuser` uid 1000 (`Dockerfile` `USER appuser`), base
  images pinned by digest; API port bound to `127.0.0.1` (`docker-compose.yml:36`).
- **Subprocess:** `app/ops/backup.py:385-403` uses fixed argv lists, no shell.
- **Ingest URL in logs:** the readiness error is a fixed message (`app/ingestion/readiness.py:35-39`);
  CLI prints only `exc` text (`app/cli.py:242-247`). GitHub masks the secret value in logs.
- **Dependencies:** `uvx pip-audit` on `uv export --frozen` output: "No known vulnerabilities found".
- **/ui** has a CSP middleware (`app/main.py:81-94`).

## Findings

**SA-001** · secrets · **P0** · out of repo (chat screenshot) · The Neon owner password
appeared in a screenshot during a chat session. Evidence: caller report; cannot verify from
the repo. Impact: full DB access (owner role, incl. `article_bodies`) for anyone holding it,
endpoint is internet-reachable. Risk of change: low (update `.env` and the GitHub secret).
Action: rotate the `neondb_owner` password in Neon, update `.env` and the `production`
environment secret `DATABASE_URL`, then run one `workflow_dispatch`. Verification: Neon
console shows a reset after the screenshot date; old password fails. Status: **unknown,
owner must confirm.**

**SA-002** · privileges · P1 · `docs/architecture.md` 5.6 · Residual risk of the accepted
one-owner-URL trade-off: one leaked value (Mac `.env`, GitHub secret, a log, a screenshot as
in SA-001) gives DDL/DROP and article text. Not reversed. Cheap mitigations that keep the
decision: (a) add required reviewers or a branch restriction (`main` only) on the
`production` environment so a workflow edit on another branch cannot read the secret;
(b) enable Neon IP allow-listing is not possible for GitHub runners on free tier (unknown),
so rely on (a) plus rotation; (c) keep Neon point-in-time restore window and regular
`news backup`; (d) enable GitHub secret scanning + push protection on the public repo.
Dependencies: SA-001. Verification: Settings > Environments > production shows protection rules.

**SA-003** · docs/behavior drift · P1 · `app/main.py:32-34` vs `docs/architecture.md` 5.6
("the Mac, the API and GitHub all use `DATABASE_URL`") · With `ENVIRONMENT=production` the
API **refuses to start** unless `API_DATABASE_URL` is set, and `assert_least_privilege`
(`app/db/session.py:77`) also refuses the owner login. So the "one string everywhere"
statement is only true for the API in development/staging (where it only warns). Section
5.3 note (`docs/architecture.md:474`) is correct but 5.6 does not mention it. Impact:
correct and safe behavior (guard must not be weakened), but a future operator may "fix"
it by weakening the guard. Action: add one sentence to 5.6 and `.env.example`: a production
API requires `API_DATABASE_URL` = `news_api_login`. Verification: doc review.

**SA-004** · config · P3 · `.github/workflows/ingest.yml` `ENVIRONMENT: production` · Only
the API reads `is_production` (`app/main.py`); for `news ingest` it has no security effect
beyond config defaults. Harmless; note so nobody assumes it enforces anything.

**SA-005** · logging · P2 · general · Uncaught exceptions during ingest would print a
traceback; psycopg messages can include host/user (not password). GitHub masks the full
secret but not substrings. Impact: low (hostname/user disclosure on a public repo's
Actions logs, which are public). Action: consider making Actions logs non-public is not
possible for a public repo; optionally wrap `news ingest` top level to log `type(exc).__name__`
only. Verification: forced failure test with a fake URL.

## Counts

P0: 1 · P1: 2 · P2: 1 · P3: 1 (total 5)
