# Architecture

Status: **approved direction (architecture A, local-first), 2026-11-23.** Ingestion, the
domain schema and the article API are not built yet. Milestone 0 (docs) is the only one
done. Every later milestone needs explicit approval before it starts.

## 1. Goal and constraints

A personal news-intelligence platform:

```
sources → ingestion → normalization → PostgreSQL → (analysis) → FastAPI → web app
```

The first source is Hespress English. More publishers will follow.

**Hard constraints:**
- The infrastructure budget is exactly **$0**. No paid hosting, and no free tier as a
  required dependency.
- The Mac is **not always online**. Ingestion is **run manually** and must pick up
  everything published since the last completed run.
- **Rights first:** what we store and do with content is decided per source, by policy.

## 2. Architecture A: local-first, manual runs

```
 Hespress EN                        Your Mac (owned, $0)
 ├ /feed?paged=N ──────────┐   ┌──────────────────────────────────────────────┐
 ├ sitemap-news.xml        ├──►│ uv run news ingest      (you run it)         │
 └ sitemap-posttype-       │   │  1 DB readiness check (fail fast, ~10 s)     │
   post.YYYYMM.xml ────────┘   │  2 advisory lock (no overlapping runs)       │
                               │  3 polite fetch: robots, 5 s delay, cond. GET│
                               │  4 RSS walk back to checkpoint               │
                               │  5 news sitemap (last ~48 h)                 │
                               │  6 archive backlog (bounded per run)         │
                               │  7 rights gate → upsert → run report         │
                               │                    │                         │
                               │        PostgreSQL 18 (Docker, :5433)          │
                               │                    │                         │
                               │ news analyze (later, local, permitted text)  │
                               │ FastAPI /api/v1 (SELECT-only role) → web app │
                               │ uv run news backup → ~/Backups (+Time Machine)│
                               └──────────────────────────────────────────────┘
 GitHub: CI only. It runs lint, types, migration check and tests against a throwaway
 Postgres. No production data, no article content, no secrets.
```

**Why this design** (options compared on 2026-11-23):
- **GitHub-hosted scheduled runners:** the filesystem is ephemeral, so they need a
  persistence layer. They can't reach a database on the Mac without exposing it to the
  internet. Scheduled runs are best effort and get disabled after 60 days of repo
  inactivity.
- **Self-hosted runner on the Mac:** adds a remote trigger and a code-execution risk.
  GitHub advises against self-hosted runners on public repos. It still needs the Mac awake.
- **Committing data to git:** not a database. Article content in a public repo is
  republication.
- **Local-first works** because the publisher itself provides catch-up paths (section 4).

**Trade-offs accepted:**
- **No remote access while the Mac is off.** PostgreSQL is never exposed to the internet.
  A VPN such as Tailscale (free tier) stays an optional extra, never a dependency.
- **Freshness depends on when you run ingest.** Articles are found late, but not lost,
  within the recovery limits in section 4.
- **Without a license, analysis only sees metadata.** That is a legal limit, not a
  technical one.

## 3. Verified facts: Hespress English (checked 2026-11-23)

| Fact | Value |
| --- | --- |
| Volume | 467 posts from Nov 1 to Nov 23, about **20 per day** |
| RSS `/feed` | RSS 2.0, `en-US`, **10 items** per page. Each item has the full body (`content:encoded`), `<category>` entries, a `dc:creator` (often a desk such as "Hespress EN" or "With MAP") and `pubDate`. |
| RSS paging | `/feed?paged=N` works. **Measured 2026-11-23: page 1,600 still returns 10 items, reaching 2025-02-24 (~21 months).** The probe stopped there (5 requests); the true limit is deeper. 16,000 items over 21 months is about 25 per day. |
| Conditional GET | Weak `ETag` and `Last-Modified`. A repeat request returns **304**. |
| Stable ID | `<guid>` is `https://en.hespress.com/?p=149695`, and the URL starts with `/149695-`. The WordPress post ID is our `external_id`. **IDs are assigned when a draft is created, so they are not in publish order.** |
| News sitemap | `sitemap-news.xml`: the last ~48 h (39 entries), with title, publish date, language and keywords |
| Archive sitemaps | `sitemap.xml` indexes `sitemap-posttype-post.YYYYMM.xml` from **2020-03** to today. Each entry has a URL, `lastmod` and an image, but **no title and no publish date**. |
| robots.txt | `Crawl-delay: 5`. Disallows `/author/`, `/wp-admin/`, `/abuse.php`, `/*?print`. Those lines sit outside a `User-agent` group, so RFC 9309 parsers ignore them. **We follow them anyway.** |
| Terms | The English edition has **no terms page**; the footer says "All rights reserved to Hespress". The main site's terms (hespress.com/conditions, in Arabic) apply in practice. See section 5. |

Re-verify this table when the publisher's behavior looks different, and update the date.

## 4. Ingestion and recovery

### 4.1 One run of `news ingest`

1. **Readiness.** Connect to PostgreSQL with a short retry, about 10 s total. If Docker or
   Postgres is down, exit non-zero with a clear message. Also refuse to run if the
   database is not at the Alembic head.
2. **Lock.** Take `pg_try_advisory_lock`. If another run holds it, exit with a message.
   One run at a time needs no job queue: no leases, no `SKIP LOCKED`, no Celery or Redis.
3. **Per feed,** in order: RSS walk (4.2), news sitemap, archive backlog (4.3).
4. **Commit after every page,** so an interruption keeps all completed work.
5. **Write an `ingestion_runs` row** and print a report: new, updated, backlog remaining,
   unrecoverable items, and duration.

### 4.2 RSS walk with a checkpoint

- Each feed stores a **checkpoint**: the newest article (external ID and `published_at`)
  seen by the last **completed** walk.
- The walk fetches page 1, 2, 3 and so on, and stops when it sees the checkpoint article.
  It also stops when items are older than `checkpoint.published_at` minus 1 day, in case
  the checkpoint article was deleted. It never stops just because it hit a known article.
- The checkpoint moves forward **only when the walk completes**. An interrupted run
  leaves it unchanged, so the next run re-walks the whole gap. Already-saved items
  upsert as no-ops, so nothing is lost.
- **First run:** a bounded bootstrap, the last 30 days by default (`--since`), not the
  whole archive.
- **Page 1 uses conditional GET.** A 304 means nothing new, and the walk ends at once.
- **Page-1 validators (ETag, Last-Modified) are saved only when the walk completes,**
  like the checkpoint. Saved early, an interrupted run would make the next run see a 304
  and skip the rest of the gap.
- **Feed ran out before the checkpoint** (a 404 or empty page, so the gap is deeper than
  the feed reaches):
  - the window is recorded in `feeds.uncovered_since` / `uncovered_until`, merged with any
    earlier gap. A merged window can over-cover; the backlog only queues URLs not already
    stored, so that is safe;
  - the checkpoint still advances.
- **Page limit** (`max_pages`, a safety cap): the walk is incomplete, nothing advances,
  and the next run walks again.
- **News sitemap** (last ~48 h) is **insert-only**: it adds articles the walk missed but
  never overwrites the richer RSS fields. When RSS later sees such an article, it enriches
  the row (author, categories).

### 4.3 Archive backlog (gaps deeper than the RSS feed)

- **When:** the walk ran out of pages, or a page returned 404 or empty, before reaching
  the checkpoint. That means the gap is older than the feed reaches.
- **Discovery:** read the monthly archive sitemaps that cover the gap. Every URL whose ID
  is not in `articles` goes into `discovered_urls`. It's a persistent backlog, and
  idempotent thanks to a unique `(source_id, external_id)`.
- **Metadata:** archive entries have no title or date. Resolution is bounded per run
  (`--max-pages`, default 100), and committed per item:
  1. Prefer the paged RSS when it still covers that period: 10 items per request.
  2. Otherwise fetch the **article page** and extract **metadata only**: title, publish
     and modified dates, author, categories. They come from `<meta property="og:*">` /
     `article:*` tags or JSON-LD. The body is not stored.
- **Bookkeeping:** each backlog row tracks `attempts`, `last_attempt_at`, `status`
  (`pending` → `resolved` | `failed` | `gone`) and `last_error`. After 5 failed attempts
  a row is `failed` and shows up in the report. Resolved rows are skipped.

**Recovery guarantees, stated honestly:**

| Time offline | Cost (5 s delay) | Guarantee |
| --- | --- | --- |
| Up to ~21 months (measured RSS depth) | 1 RSS page per 10 missed articles: 1 week ≈ 18 pages ≈ 1.5 min, 2 months ≈ 150 pages ≈ 13 min | Complete metadata |
| Beyond the RSS depth | archive backlog (milestone 6, deferred): 1 article page per missing article | Complete only if the publisher still lists the URL and serves the page |
| Unpublished or deleted by the publisher | none | Not recoverable |

**Within the measured RSS depth, a manual run recovers everything** still published since
the last completed run. This is verified by the long-downtime and interruption tests
(milestone 4) and the real depth probe.

**Run length:** a long absence means a long first run, at 5 s per page. The `--max-pages`
cap (default 500, about 42 minutes, about 5,000 articles or 6+ months) bounds it. A
capped run doesn't advance the checkpoint, so the next run simply continues.

### 4.4 Polite and safe fetching

- **Identity and robots:** a User-Agent with the repo URL as contact.
  - robots.txt is fetched once per host per run.
  - It is parsed by our own RFC 9309 parser (`app/ingestion/robots.py`).
    `urllib.robotparser` doesn't handle `*`/`$` wildcards (so it would miss
    `/*?print`) and it drops rules outside a group.
  - If robots.txt returns 4xx, there are no restrictions; if it returns 5xx or is
    unreachable, nothing on that host is fetched (RFC 9309 §2.3.1).
  - Requests to a host are spaced by max(`HTTP_MIN_DELAY_S`, `Crawl-delay`)
    (Hespress: 5 s).
- **Limits:** timeouts of 5 s to connect and 20 s total; responses capped at 5 MB.
- **Retries:** for 429, 5xx and network errors only, with exponential backoff and jitter
  (3 attempts). `Retry-After` is honored.
- **XML parsing** uses `defusedxml` for RSS and sitemaps (`app/ingestion/parsers.py`). It
  refuses entity-expansion, external-entity and DTD attacks.
  - It is strict: a malformed page raises and is recorded instead of being half-ingested.
  - `feedparser` was considered and dropped: it is lenient, untyped and redundant for
    standard WordPress RSS.
- **Feed health:** `consecutive_failures` drives the state: `active` → `degraded` after
  5 failures → `disabled` after 20 (manual re-enable).
- **Never bypass** authentication, paywalls, anti-bot measures or access restrictions.
  Feeds, sitemaps and article pages (metadata only) are the only inputs.

## 5. Rights model

### 5.1 What the terms say (hespress.com/conditions, read 2026-11-23)

- **§2:** using the site means accepting the terms.
- **§6:** Hespress owns all intellectual property, including copying rights, in all
  texts, graphics, selections and their arrangement, even general information.
- **§7:** only Hespress may license use, copying or republication of its articles.
- **§8:** any such license requires no distortion of meaning and a link back.
- The terms do not mention RSS, automated access or text and data mining.

### 5.2 Four separate acts, each needing its own permission

| Act | Status for Hespress now |
| --- | --- |
| **Access** (fetching published feeds, sitemaps and pages within robots.txt) | Acceptable at polite rates |
| **Storage** of full text | Not permitted: §6–7 reserve copying |
| **Analysis and transformation** (mining, summaries, translation, AI) | Depends on the jurisdiction's text-and-data-mining rules and on the terms. Summaries and translations are derivative works. **Paraphrasing does not remove the obligation.** Processing in memory only is **not** automatically exempt. |
| **Republication** | Needs a license (§7) with no distortion and a link back (§8) |

**Facts are not copyrightable.** Original reporting is the legitimate route to original
publications: independently verified facts, our own wording, links and short attributed
quotes where the law allows.

### 5.3 Per-source policy

| Policy | Fetch | Store | Analyse | Serve |
| --- | --- | --- | --- | --- |
| `disabled` | no | nothing new | none | existing metadata |
| `metadata_only` | yes | title, URL, dates, author, categories; excerpt up to `excerpt_max_chars` (0 = none) | stored metadata only | metadata and link-out |
| `restricted` | yes | as `metadata_only` | only under a **documented legal basis**; derived results only | derived results and link-out |
| `licensed_full_text` | yes | full text in `article_bodies`, within license scope and retention | as the license allows | as the license allows |

**Hespress:** `metadata_only` with `excerpt_max_chars = 0`.

**Enforcement:**
1. Source adapters return `content:encoded` only when the policy is
   `licensed_full_text`. Otherwise the field never leaves the adapter.
2. Only that policy writes `article_bodies`.
3. Tests run every policy against fixtures.
4. An audit query (bodies for non-licensed sources must be zero) runs in the test suite.

**What enabling full text for Hespress requires:** written permission from Hespress
covering:
- storage of full text;
- internal analysis, including machine learning and AI;
- any display or excerpting;
- commercial or non-commercial use;
- duration, revocation and retention;
- attribution format.

Record `license_ref` and `license_expires_at` on the source. A legal review is needed
before any public or commercial use. *This document is not legal advice.*

## 6. Data model (target, built in milestone 1)

```sql
sources(id, slug UNIQUE, name, site_url, language,
        rights_policy CHECK IN ('disabled','metadata_only','restricted','licensed_full_text'),
        excerpt_max_chars, license_ref, license_expires_at, active, created_at)
feeds(id, source_id FK, url UNIQUE, kind CHECK IN ('rss','news_sitemap','archive_index'),
      etag, last_modified, checkpoint_external_id, checkpoint_published_at,
      consecutive_failures, state CHECK IN ('active','degraded','disabled'),
      last_success_at, created_at)
articles(id, source_id FK, external_id, canonical_url UNIQUE, title, excerpt, author,
         categories text[], language, published_at, modified_at, first_seen_at,
         first_seen_via CHECK IN ('rss','news_sitemap','archive'), content_hash,
         search_tsv tsvector GENERATED, UNIQUE(source_id, external_id))
discovered_urls(id, source_id FK, external_id, url, lastmod, discovered_at,
                status CHECK IN ('pending','resolved','failed','gone'), attempts,
                last_attempt_at, last_error, article_id FK NULL,
                UNIQUE(source_id, external_id))
ingestion_runs(id, started_at, finished_at, status, trigger, feeds_processed,
               pages_fetched, items_new, items_updated, backlog_resolved,
               backlog_remaining, error_class, error_detail)
-- Later, only when needed:
article_bodies(article_id PK FK, body_text, license_ref, stored_at, expires_at)
analyses(id, article_id FK, analyzer, analyzer_version, result jsonb, created_at,
         UNIQUE(article_id, analyzer, analyzer_version))
```

- **Timestamps:** all are `timestamptz`, stored in UTC.
- **Indexes:**
  - `articles(published_at DESC, id DESC)` for keyset pagination;
  - GIN on `search_tsv` (`english` config, title and excerpt);
  - `discovered_urls(status) WHERE status = 'pending'`;
  - later, `pg_trgm` on `title` for cross-publisher duplicates.
- **Content hash:** `content_hash` over the stored fields detects publisher edits, which
  update the row and `modified_at`.
- **Database roles** (`scripts/db_roles.sql`, idempotent, re-run after each migration):
  - the schema owner is the `POSTGRES_USER` (`news`), which runs migrations;
  - `news_ingest` reads and writes data, cannot change the schema, and has no access to
    `alembic_version`;
  - `news_api` has SELECT only.

  Both are NOLOGIN group roles. Login users with their own passwords are added when a
  process needs them (the API in milestone 7).
- **Retention:** articles are kept indefinitely (metadata). `ingestion_runs` for 180 days.
  Resolved `discovered_urls` for 30 days. Bodies only as their license allows.
- **Deliberately not stored:** raw feed payloads, because they contain full text.

## 7. API (milestone 7)

- **Endpoints:** `GET /api/v1/sources`, `/feeds`, `/articles`, `/articles/{id}`; later
  `/articles/{id}/analysis`. They only read stored data; nothing fetches publishers on a
  request.
- **Pagination:** keyset on `(published_at DESC, id DESC)` with an opaque cursor; `limit`
  is capped at 100. Never `OFFSET`.
- **Filters:** source, category, language, date range, and `q` (Postgres full-text
  search, best effort).
- **Caching:** `ETag` and `Cache-Control` headers.
- **Access:** connects as the SELECT-only `news_api` role. Local only. API keys and rate
  limiting are added only if remote access is ever enabled.

## 8. Backups

- **`uv run news backup`:** `pg_dump -Fc` from the db container to `~/Backups/news/`,
  outside the repo, kept 14 copies. Time Machine provides a second copy.
- **Reminder:** `news ingest` warns when the newest backup is older than 7 days.
- **`uv run news restore-test`:** restores the newest dump into a scratch database and
  compares row counts per table. **A backup only counts once a restore test has passed.**
  Run it monthly.

## 9. CI and GitHub

- `ci.yml` stays CI-only: ruff, mypy, `alembic upgrade head` + `alembic check`, and
  pytest against a throwaway Postgres service. There is no ingestion workflow and no
  self-hosted runner.
- Migrations are applied locally with `uv run alembic upgrade head`. `news ingest`
  refuses to run when the database is behind.
- No GitHub secrets are needed.
- Data never enters git: `.gitignore` covers `backups/`, `*.dump`, `*.sql.gz` and `.env`.
  pre-commit blocks large files and private keys. Test fixtures are synthetic.

## 10. Roadmap

Every milestone's definition of done: ruff, mypy (strict) and the full test suite pass,
`alembic check` is clean, and the docs are updated. **Each milestone needs approval
before it starts.**

| # | Milestone | Files | Behaviour | Tests |
| --- | --- | --- | --- | --- |
| 0 ✅ | Docs and config | `docs/architecture.md`, `CLAUDE.md`, `.env.example`, `.gitignore` | Docs match verified facts; `.env.example` works copied as-is | n/a (doc review) |
| 1 ✅ | Schema, roles, seed | `app/models/*`, `alembic/versions/*`, `scripts/db_roles.sql` | Tables, constraints, indexes; Hespress source and feeds seeded | Constraints and dedup uniqueness; each role can't exceed its permissions |
| 2 ✅ | Rights gate | `app/ingestion/policy.py`, `normalize.py` | `apply_policy` is the only path from a fetched item to a storable record; fail-safe license downgrades | Every policy; expired or missing license; record type and `articles` table cannot hold a body (the bodies-audit query moves to when `article_bodies` exists) |
| 3 ✅ | Polite fetcher | `app/ingestion/fetch.py`, `robots.py` | Conditional GET, robots, delay, limits, retries | `httpx.MockTransport`: 304, 429 with Retry-After, 5xx backoff, timeout, size cap, disallowed path |
| 4 ✅ (depth probe pending) | Hespress adapter and RSS walk | `app/ingestion/parsers.py`, `sources/wordpress.py`, `store.py`, `walk.py` | Parse, normalize to UTC, guid → ID, checkpoint walk | Malformed XML, repeated runs (idempotent), interrupted run resumes to checkpoint, deleted checkpoint, duplicates across feed and sitemap; **measure real RSS depth with a few probes** |
| 5 ✅ | `news ingest` command | `app/cli.py`, `app/ingestion/runner.py`, `readiness.py`, `pyproject.toml` script | Readiness, lock, run report, migration-head check | DB down → clean failure; concurrent run refused; `ingestion_runs` recorded |
| 6 ⏸ deferred | Archive backlog | `app/ingestion/backlog.py`, `page_metadata.py` | Bounded, checkpointed, idempotent recovery | Long downtime beyond RSS depth; partial recovery across runs; failed/gone rows; body never stored |
| 7 | Read API | `app/api/v1/*`, `app/schemas/*` | Sources, feeds, articles, search | Pagination stable under inserts; filters; ETag; role is SELECT-only |
| 8 | Backups | `app/ops/backup.py`, CLI commands | Backup, retention, restore test | Restore into a scratch DB matches counts; retention prunes |
| 9 | Analysis framework | `app/analysis/*` | Local analyzers on permitted text, versioned | Idempotent per analyzer version; policy respected |
| 10 | Frontend | separate | Discovery UI with link-out and attribution | n/a |

## 11. Risks and open questions

| Risk or question | Status |
| --- | --- |
| Jurisdiction of the operator (decides the text-and-data-mining rules) | **Open: owner to answer** |
| Written permission from Hespress for full text | **Open: none yet.** Design stays `metadata_only`. |
| Real RSS paging depth | **Measured:** at least 1,600 pages (~21 months) on 2026-11-23. The archive backlog is deferred until a source needs it. |
| Publisher changes (feed format, sitemap plugin, paging) | Detected through run reports and feed states |
| Fetching article pages for metadata | Approved for metadata only. Re-check robots.txt and terms before milestone 6. |
| The Mac is a single point of failure | Backups with restore tests, plus Time Machine |
| Hostile or malformed XML | `defusedxml`, size caps, malformed-input tests |
| Public commit history exposes the owner's email | Open, owner's decision |
| Project scope | Personal and private for now. Re-review the rights model before any public use. |
