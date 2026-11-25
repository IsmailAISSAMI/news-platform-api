# Review: wave-1 recovery (uncommitted diff, 2026-11-25)

Out of scope (being edited elsewhere): README.md, CLAUDE.md, docs/**, .env.example,
.github/workflows/ingest.yml, app/api/v1/analysis.py.

## Checks re-run by the reviewer (own DB news_rev_test, dropped afterwards)
- `uv run ruff check .` -> All checks passed; `ruff format --check .` -> 157 files already formatted
- `uv run mypy app tests alembic` -> Success: no issues found in 126 source files
- `uv run python scripts/check_repo_hygiene.py` -> Repository hygiene: OK
- `uv run pytest -q` -> 788 passed, coverage 95.90% (no skips reported)
- gitleaks v8.30.1 linux_x64 SHA-256 checked against the upstream checksums file: matches ci.yml.

## Verdicts

B-D2 (llm.py network-error cap): REQUEST CHANGES (small).
- Never increases requests: every attempt still goes through reserve/ledger; give-up records
  the last reservation as failed (llm.py `_give_up_offline`), test asserts 5 requests,
  4 busy, 1 failed, 0 pending, used_this_run 0. Reset on any HTTP answer is correct.
- Gap: `httpx.ConnectTimeout` is a subclass of `httpx.TimeoutException` (verified: True), so
  it takes the uncapped "timeout" branch AND resets the counter. An offline Mac whose
  connect attempts time out (no route, captive Wi-Fi) still retries forever, reserving daily
  budget: exactly what D2 is meant to stop. Fix: treat ConnectTimeout (and PoolTimeout) as
  network errors; do not reset the counter on a timeout (only on an HTTP response). Add a
  test with ConnectTimeout.

B2 (IA-011 output never in error text): APPROVE. `_error_summary` uses
`include_input=False`; sentinel tests prove it.

Schemas Optional widening (app/schemas/analysis.py): APPROVE. app.js already falls back
(`item.headline || item.title`, `o.headline || d.title`, `b.what?.main_event ? ... :
notStated()`, tone now guarded). New end-to-end test covers nulled headline/main_event/
tone explanation on list and detail. New required `BriefingSummary.source` is populated
(test asserts it) but lives in analysis.py (out of scope; B9 must keep it).

UI (app.js): APPROVE. Removed fallbacks (`source_name`, `b.unverified_fields`, `d.url`)
are fields the API never sends per schemas; tests pin it. Banner now keys on
`removed_items > 0`, wording covers copied text.

CLI report line (tests/test_cli.py): APPROVE. Expectation change is additive (adds
"N unchanged"); the old string was not wrong, it omitted an existing counter. Behavior
change is report text only.

Test rename test_api.py (`test_no_response_ever_carries_a_body` ->
`test_article_list_items_have_no_body_keys`): APPROVE. Body unchanged; the old name
overclaimed (only the list route). Broader coverage is added in
test_api_briefings.py (BODY_SENTINEL).

test_workflows.py: APPROVE. Assertions strengthened (pooler branch must `exit 1`, ordering).

B-D3 backup local restore: APPROVE. Remote tools -> `restore_target` builds local
`docker compose exec -T db` tools with user from printenv, env None (no password); unit
test asserts every argv uses the local prefix, no host, no password; scratch DB is dropped
in `finally`; container down fails before anything is created (1 call). Note: if the
pre-create dropdb fails nothing remains; if createdb succeeds and restore fails, the
finally drop runs. psql query is a fixed string.

Readiness pooler refusal: APPROVE. `"-pooler" in host` is checked before connecting, in
every `wait_for_database` caller (cli.py:234,286,492,510). False positive only for a host
literally containing "-pooler"; acceptable and the message explains it.

conftest autouse fixture: APPROVE. Only unsets PG_TOOLS_COMMAND/REQUIRE_BACKUP_TESTS for
non-integration tests; does not touch the DATABASE_URL rewrite or the test-DB guard.

ci.yml: APPROVE. Pinned version + verified checksum, full-tree `gitleaks dir --redact`.
Lint/Type steps removed but pre-commit (locked via uv.lock) runs `uv run ruff check --fix`,
`uv run ruff format` (fail on modification via --show-diff-on-failure) and
`uv run mypy app tests alembic` with pass_filenames false: equivalent coverage.

.gitignore / .dockerignore `* [0-9]`, `* [0-9].*`: APPROVE. `git ls-files` has no
matching tracked file. Would ignore future names like "notes 2.md"; acceptable given
the iCloud rule. .dockerignore excluding docs/engineering/.claude: README.md still
included (needed by pyproject readme).

pyproject.toml: APPROVE. package-data ships app/web/static; pydantic/starlette made
explicit (already transitive, no new packages in uv.lock). Removed comment was an
orphan note, no legal/quota meaning.

budget.py `CONCURRENCY_CEILING` removal: APPROVE. grep finds no users; `CEILINGS` unchanged.

config.py default from GEMINI_BASE_URL: APPROVE (same value, one source).

walk.py downgrade warning: APPROVE. Logs policy, slug, URL, reason only; test proves no body.

fake_ledger.py SENT_OUTCOMES: APPROVE. Aligns fake with real ledger semantics;
tests/integration/test_ledger_contract.py exists.

docker-compose digest pin: APPROVE.

.claude/settings.json: APPROVE. alembic upgrade moved allow -> ask (owner D5); more .env
variants denied. Nothing loosened.

No security or quota guard weakened, except the B-D2 ConnectTimeout gap above.

## Resolution of REQUEST CHANGES (B-D2), 2026-11-25

Fixed by the lead in `app/analysis/llm.py`: `ConnectTimeout` and `PoolTimeout` now count
as network errors (`_is_network_error`), and a read/write timeout no longer resets the
consecutive count (only an HTTP answer does). Tests added first and shown failing on the
reviewed code: `test_connect_timeouts_count_as_network_errors`,
`test_a_read_timeout_does_not_reset_the_network_error_count`. Result: 52 passed in
`tests/analysis/test_llm.py`; ruff (incl. C901 ≤ 12), format and mypy clean. B-D2: resolved.
