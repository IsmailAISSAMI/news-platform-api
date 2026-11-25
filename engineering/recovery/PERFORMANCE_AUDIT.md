# Performance audit (2026-11-25, static analysis, baseline commit `3274239`)

Scope: Neon storage and connections, API queries vs indexes, ingestion request counts,
analysis token budget, engine settings. Method: reading code and migrations only. No
connection to Neon, no tests, no Gemini, no ingest. No numbers below were measured;
every proposal says how to measure it first. Owner decisions (architecture 5.4, 5.5, 5.6)
are treated as requirements.

## Findings

**PA-001 · Neon storage · P1 · `app/ops/backup.py:283-291`**
`news restore-test` creates the scratch database `<db>_restore_check` **on Neon** (5.6:
"The scratch restore database is created and dropped on Neon") and restores the whole
dump into it. Evidence: `tools.create_db(scratch)` then `tools.restore(scratch)`.
Impact: peak storage is about 2x the data size during every restore test, and Neon's
history (point-in-time restore window) may keep the written pages after the drop. Once
data passes ~half the plan limit, restore-test itself can hit the storage cap and the
project can go read-only. Risk of change: low, but it touches the 5.6 decision, so the
owner decides. Recommendation: restore the test into the local Docker container instead
(it already exists for tests), or at least document the 50 % ceiling. Verification:
`SELECT pg_database_size(current_database())` before/after, and Neon console storage
graph during one `restore-test`.

**PA-002 · Neon storage · P2 · `app/analysis/pipeline.py:206,230`; `app/models/article_analysis.py:76-88`**
Every analysis row keeps `raw_output` (the full model answer, also on success, line 230)
plus four derived JSONB copies of similar content (`analysis`, `cleaned`, `briefing`,
`evidence`) and an append-only `attempt_history` (line 496-502, `||` on every attempt).
Re-analysis under a new `analyzer_version` adds a new row (UNIQUE on
`(article_id, analyzer_version)`, line 43), so each prompt/schema bump multiplies this.
Impact: unknown in bytes; likely the largest table per article after `article_bodies`.
Recommendation (needs owner approval, it is retention): after measuring, consider
nulling `raw_output` for `succeeded` rows older than N days and deleting rows of
superseded `analyzer_version`s. Verification:
`SELECT analyzer_version, count(*), avg(pg_column_size(raw_output)), avg(pg_column_size(analysis)+pg_column_size(cleaned)+pg_column_size(briefing)), max(jsonb_array_length(attempt_history)) FROM article_analyses GROUP BY 1;`
plus `pg_total_relation_size('article_analyses')`.

**PA-003 · Neon storage · P2 · growth model unknown; plan limit unverified**
Growth sources: `articles` + `article_bodies` (~20-25 articles/day, architecture section 3),
`article_analyses` (PA-002), `api_calls` (up to 1,000 rows/day, `budget.py:50` area),
`ingestion_runs` (1 row/day from the daily workflow plus manual runs),
`analysis_runs` (1 per `analyze`). None has retention. `discovered_urls` is bounded by
the archive (milestone 6 deferred), not unbounded. `api_calls` and `ingestion_runs` rows
are small (ints, short text); at 1,000 calls/day `api_calls` is roughly tens of MB/year
(estimate, unmeasured). Also: the "1 GB" figure in the brief is not stated in
architecture 5.6, and Neon's free-plan storage and history-retention terms change; treat
the real cap as **unknown** until read in the Neon console. Recommendation: record a
monthly size snapshot and project; decide retention only if the projection crosses ~50 %
within 3 years. Verification:
`SELECT relname, pg_total_relation_size(relid) FROM pg_statio_user_tables ORDER BY 2 DESC;`
run monthly (read-only), plus Neon console "Storage" and plan page.

**PA-004 · API query · P1 · `app/api/v1/analysis.py:10,128-151`**
`/briefings` walks `articles` in `(published_at DESC, id DESC)` order and does an inner
`JOIN LATERAL ... ON true` (line 149) to the latest briefing. Articles with no briefing
are dropped by the join, so PostgreSQL must probe every article until it has `limit+1`
matches. The docstring (line 10) claims "about `limit` index probes", which holds only
when most articles are analysed. Analysis is manual (5.6) while ingest is daily, so the
analysed fraction can be low; with 1 in K articles analysed a page costs ~K x limit
probes, and with none analysed beyond some point it scans the whole table, bounded only
by `statement_timeout` 5 s (`app/db/session.py:17`). Recommendation: drive the query from
the partial index instead (select distinct-on article_id from `article_analyses` filtered
to briefings, ordered by the article's `(published_at, id)`), or add a
`has_briefing`-style filter. Verification: on the local test DB, seed 20k synthetic
articles with 1 %, 10 %, 100 % briefed, run `EXPLAIN (ANALYZE, BUFFERS)` on the
generated SQL for page 1 and a deep cursor page; compare buffers before/after.

**PA-005 · API query · P2 · `app/api/v1/catalog.py:120-137`**
The articles list always orders by `(published_at DESC, id DESC)` (index
`ix_articles_published_at_id`, `app/models/article.py:36`). Selective filters
(`category` via GIN `ix_articles_categories`, `q` via GIN `ix_articles_search_tsv`) force
the planner to choose between the GIN index plus sort, or the ordered index plus filter.
A rare category on the ordered index means a long scan, again capped only by the 5 s
timeout. `language` and `source` have no index (fine: one source, one language today).
Impact at current size (~thousands of rows): negligible. Recommendation: none now;
measure first. Verification: `EXPLAIN (ANALYZE, BUFFERS)` with synthetic 50k rows for a
rare category and a rare `q` term.

**PA-006 · Indexes · P3 · `app/models/api_call.py:40`**
`ix_api_calls_run_id` serves no read: `run_id` is only written (`ledger.py:96`,
`pipeline.py:324`); its only use is the FK `ON DELETE SET NULL` when an `analysis_runs`
row is deleted, which no code does. Conversely the per-minute seeding query filters
`requested_at > clock_timestamp() - 60 s` (`ledger.py:134-145`) with no index on
`requested_at`, so it is a seq scan of all `api_calls` at each client start. Both are
tiny today. Recommendation: replace the run_id index with one on `requested_at` only if
measurements show the scan matters. Verification: `pg_relation_size` of both indexes;
`EXPLAIN ANALYZE` of the seeding query with 365k synthetic rows.
Other indexes all serve a query or an integrity rule (unique keys; GINs for `q` and
`category`; partial briefing index for the LATERAL; `ix_discovered_urls_*` on a deferred,
near-empty table cost ~nothing).

**PA-007 · Engine/pool · P2 · `app/db/session.py:41-46`**
`create_async_engine` uses SQLAlchemy defaults (QueuePool 5 + overflow 10, no
`pool_recycle`) with `pool_pre_ping=True`. On Neon: (a) pre-ping adds one round trip
per checkout (Frankfurt latency) but correctly discards connections killed when compute
scales to zero, so keep it; (b) the API's pooled idle connections may keep the compute
from suspending or be cut on suspend; which one happens is **unknown** (check Neon docs
for the current plan); (c) no `pool_recycle`, so very old connections rely on pre-ping
alone. The ingest must use the direct (non-pooler) endpoint for advisory locks (5.6), so
it should not move to PgBouncer. Recommendation: for the API, consider `pool_size=2`,
`max_overflow=3`, `pool_recycle=300`; keep pre-ping. Verification: Neon console
"compute active time" for a day with the API idle, before/after; count
`SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()` (read-only).

**PA-008 · Ingestion requests · P3 (no issue found) · `app/ingestion/fetch.py:146-148,256`; `app/ingestion/runner.py:94`**
Conditional GET sends `If-None-Match`/`If-Modified-Since`; delay is
`max(http_min_delay_s=1.0, Crawl-delay)` = 5 s for Hespress; responses capped at 5 MiB
(`config.py:45`); redirects capped at 5. Steady state daily run: robots.txt + feed page 1
(304 if nothing new) + ~3 feed pages for ~25 new items (10 items/page) + 1 news sitemap
= about 5-6 requests, ~30 s. Worst case is bounded by `max_pages=500` (~42 min at 5 s),
which only a long gap or `--since-days` reaches. Verification: the run report and
`ingestion_runs` row count requests per run; compare with this estimate after a few
daily runs.

**PA-009 · Analysis tokens · P3 · `app/analysis/budget.py:44-84`; `app/analysis/llm.py:295`**
Ceilings: 20 req/min, 10,000 input tokens/min (Google: 30 and 16,000), 1,000/day.
Estimate is `bytes // 3 + 1`, conservative for English (~4 bytes/token), so real usage
is likely ~25 % under the estimate. Throughput is bound by tokens, not by concurrency
8: an article of ~12 KB prompt+body (~4k estimated tokens, unmeasured) gives ~2-3
requests/min, so concurrency above 3 adds nothing. An input over 10,000 estimated
tokens (~30 KB) is refused up front (`RequestTooLargeError`), not retried: correct.
Recommendation: none to the limits (owner rule). Verification:
`SELECT avg(est_input_tokens), max(est_input_tokens), avg(input_tokens::float/NULLIF(est_input_tokens,0)) FROM api_calls WHERE outcome='answered';`
and `uv run news gemini-usage`.

## Counts by priority

| Priority | Count | IDs |
| --- | --- | --- |
| P0 | 0 | |
| P1 | 2 | PA-001, PA-004 |
| P2 | 4 | PA-002, PA-003, PA-005, PA-007 |
| P3 | 3 | PA-006, PA-008, PA-009 |
