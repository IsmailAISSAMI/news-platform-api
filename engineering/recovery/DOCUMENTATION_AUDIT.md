# Documentation audit (2026-11-25)

Scope: README.md, CLAUDE.md, docs/architecture.md, docs/analysis.md, docs/decisions/*.md,
.env.example, scripts/*.sql headers, behaviour-documenting docstrings. Read-only; checked
against app/core/config.py, app/main.py, app/cli.py (`news --help`, `ingest --help`,
`analyze --help`), docker-compose.yml, .github/workflows/*.yml. `.env` was not read.
Owner decisions (architecture 5.4, 5.5, 5.6) and rights text are requirements, not findings.

## Findings

**DOC-001** · contradiction (docs vs code) · **P1** · CLAUDE.md:180-190, architecture.md:469-476, README.md:356-358, .env.example (absent)
- Description: docs say restricted logins / `API_DATABASE_URL` are "optional and unused by
  default; everything connects with DATABASE_URL". Code: `app/main.py:32-34` raises
  `PrivilegeError("production requires API_DATABASE_URL ...")` when `ENVIRONMENT=production`.
  `.env.example` does not document `API_DATABASE_URL`; `docker-compose.yml` `api` does not pass it.
- Impact: following the docs (one URL) with `ENVIRONMENT=production` makes the API refuse to
  start. Also conflicts with owner decision 5.6 "one connection string everywhere".
- Risk of change: the guard is a security control ("never weaken it", CLAUDE.md). Owner must
  decide: document the production exception, or change code. Do not silently edit either.
- Action: owner decision; then align CLAUDE.md, architecture 5.6/6, README, .env.example.
- Verify: `grep -n api_database_url app/main.py .env.example`.

**DOC-002** · stale-as-current · **P2** · README.md:129-131
- "run `news ingest` whenever you like ... There is no scheduler to set up." contradicts
  setup step 8 (README.md:103-106) and `ingest.yml` cron `47 22 * * *`.
- Action: say the daily GitHub run is the default and manual runs are optional.

**DOC-003** · stale-as-current · **P2** · docs/decisions/security.md:10
- Rationale "PostgreSQL is never exposed to the internet (CLAUDE.md)": since 5.6 Neon is
  internet-reachable (architecture.md:385; CLAUDE.md:218 now says so).
- Action: add a dated note pointing to 5.6; keep the S1 decision itself.

**DOC-004** · stale status · **P2** · docs/architecture.md:3, :31 heading
- "Status: approved direction (architecture A, local-first)" and section 2 title "local-first,
  manual runs" read as current; a superseded note exists only at :33-35 and diagram footnote :59.
- Action: status line should name 5.6 as current; retitle section 2 as historical.

**DOC-005** · stale status · **P2** · docs/decisions/model-selection.md:7
- Header line still says `Status: adopted` for Qwen/LM Studio, under a "Superseded" banner.
- Action: `Status: superseded 2026-11-24 (architecture 5.5)`.

**DOC-006** · stale design text · **P2** · docs/analysis.md:87-132, 303-323, 336
- LM Studio, `LLM_MODEL`, concurrency 1, model fingerprints, "LM Studio up?" readiness.
  Banner at :3-5 covers it, but the design body still reads as the plan. `LLM_MODEL` does
  not exist (config uses `GEMINI_MODEL`, config.py:55).
- Action: move the LM Studio design/measurements into a "History" section or mark each
  table row; keep measurements (they are evidence for decisions).

**DOC-007** · broken path · **P3** · docs/architecture.md:634
- `app/ingestion/backlog.py` does not exist (milestone 6 deferred). Mark as "planned".

**DOC-008** · stale framing · **P3** · CLAUDE.md:9-10, architecture.md:26-27
- "Mac is not always online" is kept as a constraint; with ingest on GitHub it no longer
  drives design. Keep but qualify ("why ingest runs on GitHub, not the Mac").

**DOC-009** · duplication · **P2** · CLAUDE.md, architecture.md, README.md
- Measured mentions (README / CLAUDE / architecture): `article_bodies` 0/5/13,
  `metadata_only` 1/2/7, `keyset` 0/1/6, `advisory lock` 0/1/4, `rights_basis` 0/2/5,
  Gemini limits (20 rpm, 10,000 tpm, 1,000/day) 1/1/2 + .env.example,
  `~/Backups/news` 3/2/3, `5433` 1/2/2 + .env.example + compose, roles/logins 0/1/3.
  The role allowlist and privilege guard are written out in full in both CLAUDE.md:176-190
  and architecture.md:460-476; the Gemini limits in four places (drift risk on any change).
  Command lists: README "Everyday commands" and CLAUDE.md "Commands" overlap ~80%.
- Action: target structure below.

**DOC-010** · verbosity (kept, report only) · **P3** · CLAUDE.md (256 lines)
- Rights/legal and owner-decision text is long; per instructions only flagged. Shortening
  needs owner approval.

## Verified correct (no action)
- `news` subcommands and flags in CLAUDE.md/README match `--help` (ingest default
  `--since-days 30`, `--max-pages` 500, analyze concurrency 1..8).
- `.env.example` Gemini defaults match config.py:59-66 and CEILINGS; DATABASE_URL /
  TEST_DATABASE_URL / 5433 match compose and conftest description.
- `ingest.yml` (22:47 UTC, single `DATABASE_URL` secret, `--since-days 1`, pooler/sslmode
  guard) matches README step 8, CLAUDE.md:82-86, architecture 5.6 and 9.
- `ci.yml` runs pre-commit, mypy, alembic upgrade+check, pytest as architecture 9 says.
- scripts/db_roles.sql and db_login_passwords.sql headers use Neon commands consistent with 5.6.
- No other broken relative links in the audited files. No LM Studio references in `app/`.

## Target structure (who owns what)

| Topic | Owner file | Others |
| --- | --- | --- |
| Setup, everyday commands, troubleshooting, user-facing rights summary | README.md | link only |
| Agent rules: hard rules (rights, geography, budget), invariants as one-liners, gotchas, definition of done | CLAUDE.md | link to architecture sections for detail; no role allowlists or limit numbers copied |
| Design, data model, roles/grants, limits (numbers), backups, CI, roadmap, owner decisions | docs/architecture.md | single source for numbers |
| Analysis design + measurement history | docs/analysis.md | current state in architecture 5.5 |
| Point decisions with status | docs/decisions/*.md | |
| Setting names and defaults | .env.example (from config.py) | |

## Counts by priority
P0: 0 · P1: 1 · P2: 6 · P3: 3 (total 10)
