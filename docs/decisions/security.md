# Decision: security baseline

**Date:** 2026-11-25 · **Owner:** project owner · **Status:** adopted

## S1. The API binds to 127.0.0.1 by default

- **Decision:** the API listens on `127.0.0.1` only, unless the owner changes the bind
  setting. It is not exposed publicly (no public host, tunnel or LAN bind) without a
  separate decision recorded in this file.
- **Why:** the Mac is not always online, PostgreSQL is never exposed to the internet
  (`CLAUDE.md`), and the API has no authentication layer.
- **Open:** no authentication exists. Before any exposure, the owner decides on
  authentication and records it here. Next step: none until then.

## S2. The API role cannot read private tables

- **Decision:** the `news_api` role is SELECT-only and its connections set
  `default_transaction_read_only=on`. It cannot read `article_bodies`,
  `article_analyses`, `api_calls` or `analysis_runs`.
- Its only analysis object is the `analysis_briefings` view (`SELECT` granted): briefings
  without evidence quotes or unverified items. A view runs with its owner's privileges,
  so this grants nothing on `article_analyses`.
- **Enforcement:** `scripts/db_roles.sql` (re-run after each migration) and the tests
  (`tests/integration/test_database_security.py`).
- The API never gets a read-write engine.

## S3. The quota ledger is the database table `api_calls`

- **Decision:** every Gemini request is counted in `api_calls` before it is sent. The
  request budget checks this table, and `news gemini-usage` reads it.
- **How** (`app/analysis/ledger.py`): the slot is reserved under a Postgres advisory lock
  and committed before sending, as a `'failed'` placeholder updated to the real outcome
  afterwards. A crash in between over-counts, never under-counts. The daily cap is counted
  per model; only one model is allowed, so it is the project-wide cap.
- **Why:** the count survives restarts, and a failed request still counts.
- The table is private (S2). Architecture section 5.5 has the limits.

## S4. Secrets

- **Decision:** secrets (database passwords, `GEMINI_API_KEY`) live only in `.env`. That
  file is git-ignored, and no tool or document reads it.
- `.env.example` holds placeholders only and must work when copied as-is. Real values never
  go there.
- **Rotation rule:** any credential that appears in terminal output, logs, chat, a
  screenshot or a commit must be rotated, even if it was removed afterwards. Rotate the
  Google key in the Google Cloud console, and database passwords in `.env` followed by a
  re-run of `scripts/db_roles.sql`.
