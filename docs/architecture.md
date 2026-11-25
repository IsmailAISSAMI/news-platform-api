# Architecture

Status: **approved direction (architecture A, local-first), 2026-11-23. Current state as
of 2026-11-25:**
- **Built:** the schema and roles, the rights gate, the polite fetcher, the Hespress RSS
  walk and news sitemap, `news ingest` (including `--rewalk-days`), the read API
  (`/api/v1`), backups with restore tests, AI analysis on the Gemini API free tier
  (`news analyze`, `news lab`, `news gemini-usage`, `news eval`), briefings and the `/ui`
  briefing screen (milestones 0-5 and 7-10, section 10).
- **Deferred:** the archive backlog (milestone 6).
- Every new milestone still needs the owner's explicit approval before it starts.

## 1. Goal and constraints

A personal news-intelligence platform:

```
sources → ingestion → normalization → PostgreSQL → (analysis) → FastAPI → web app
```

The first source is Hespress English. More publishers will follow.

**Hard constraints:**
- The infrastructure budget is exactly **$0**. No paid hosting. No free tier as a
  required dependency, **except Neon** (owner decision, section 5.6).
- The Mac is **not always online**. Ingestion runs **daily on GitHub Actions** against
  Neon (section 5.6), or by hand, and must pick up everything published since the last
  completed run.
- **Rights first:** what we store and do with content is decided per source, by policy.

## 2. Architecture A: local-first, manual runs

> **Superseded in part on 2026-11-25 (section 5.6):** the database is on Neon and a
> scheduled GitHub Actions workflow runs the daily ingest. The diagram and trade-offs
> below record the original design; where they conflict, section 5.6 wins.

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
                               │        PostgreSQL 18 (Docker, :5433)*         │
                               │                    │                         │
                               │ news analyze (Gemini API free tier, 5.5)     │
                               │ FastAPI /api/v1 (SELECT-only role) → web app │
                               │ uv run news backup → ~/Backups (+Time Machine)│
                               └──────────────────────────────────────────────┘
 GitHub: CI only. It runs lint, types, migration check and tests against a throwaway
 Postgres. No production data, no article content, no secrets.
```

\* Since 2026-11-25 the database is on Neon and the local container is for tests only
(section 5.6).

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
   The lock is session-level, so it is taken and released on a **dedicated AUTOCOMMIT
   connection** held for the whole run (`app/ingestion/runner.py`). The ORM session
   returns its connection to the pool on every commit, so locking through it could
   silently unlock.
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
- **First run:** a bounded bootstrap, the last 30 days by default (`--since-days`), not the
  whole archive.
- **Re-walk (`--rewalk-days N`):** re-reads the last N days to fill in bodies. It
  ignores the checkpoint and the page-1 validators, and it **never moves the checkpoint**
  or saves validators.
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
- **Limits:** timeouts of 5 s to connect and 20 s total. The total timeout bounds the
  whole attempt (connect, headers and the full body), so a slow-drip body cannot hold a
  run forever. Responses are capped at 5 MB.
- **Redirects** are followed by `PoliteFetcher` itself, not by httpx
  (`follow_redirects=False`): at most 5 hops (`MAX_REDIRECTS`), and robots.txt and
  per-host spacing are re-checked on **every hop**, so a redirect can never reach a
  disallowed URL.
- **Retries:** for 429, 5xx, network errors and total timeouts only, with exponential backoff and jitter
  (3 attempts). `Retry-After` is honored.
- **XML parsing** uses `defusedxml` for RSS and sitemaps (`app/ingestion/parsers.py`). It
  refuses entity-expansion, external-entity and DTD attacks, and we also pass
  `forbid_dtd=True`, so any `<!DOCTYPE>` is rejected.
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
| `restricted` | yes | metadata as `metadata_only`, plus full text **privately** in `article_bodies`; requires a recorded `rights_basis` (database-enforced) | internal analysis, including AI | metadata, derived results and link-out; **never the text** |
| `licensed_full_text` | yes | full text in `article_bodies`, within license scope and retention | as the license allows | as the license allows |

**Hespress:** `restricted` with `excerpt_max_chars = 0` since 2026-11-23 (section 5.4). It was
`metadata_only` before.

**Enforcement:**
1. The rights gate (`app/ingestion/policy.py`) releases a body only under `restricted`
   with a non-empty `rights_basis`, or `licensed_full_text` with a valid license.
   Anything doubtful falls back to `metadata_only`.
2. The database refuses `restricted` without a `rights_basis`, and `licensed_full_text`
   without a `license_ref`.
3. Bodies are stored as plain text with paragraph breaks (for AI context) in
   `article_bodies`, never in `articles`.
4. The API never selects bodies, and the `news_api` role has no access to
   `article_bodies`. Tests check both.
5. `news ingest --rewalk-days N` re-reads a window to fill in bodies for articles stored
   earlier.

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

### 5.4 Owner decision: private full text for Hespress (2026-11-23)

- **Decision:** the owner chose to store Hespress full text privately, for non-commercial
  educational AI analysis, without asking Hespress for permission.
- **Recorded:** in `sources.rights_basis` by migration `bb03517b5d1e`.
- **Advice given and declined:** obtaining a license; relying on a text-and-data-mining
  exception confirmed by a lawyer for the owner's jurisdiction.
- **Known tension:** Hespress's terms (section 5.1) reserve copying. Educational and
  private-use exceptions differ by country. This is the owner's decision, not a legal
  clearance.
- **Current rule (since 2026-11-24):**
  - full text (`article_bodies`) **and every analysis derived from it** (`article_analyses`,
    `analysis_runs`, claims, quotes, places, entities) are private. They are never shown by
    the API, exported, published or committed. Analyses are private, not only the bodies;
  - the `news_api` role cannot read `article_bodies`, `article_analyses`, `api_calls` or
    `analysis_runs` (enforced by `scripts/db_roles.sql` and tests);
  - **the one exception:** AI analysis sends article text to Google's Gemini API, free
    tier. Section 5.5 is the decision that covers it. No other path sends article text
    off the owner's machine;
  - text is never committed (test fixtures are synthetic);
  - backups contain it and stay private (mode 600, outside the repo and iCloud);
  - publishable AI output must be original, with no reproduced or closely paraphrased
    passages.
- **To reverse:** set the source back to `metadata_only` (collection stops), then
  `DELETE FROM article_bodies` for that source's articles. Stored analyses are handled as
  in section 5.5.

### 5.5 Owner decision: AI analysis on the Gemini API free tier (2026-11-24)

- **Decision (owner, 2026-11-24):** AI analysis runs on Google's Gemini API, model
  **Gemma 4 31B (`gemma-4-31b-it`), free tier only**. LM Studio and the local models are
  retired. The owner compared them on the same articles and preferred Gemma 4 31B.
- **Data sent:** article text (as numbered paragraphs) and the prompt are sent to Google.
  This is the only path by which article text leaves the owner's machine.
- **Free-tier data-use consequence (accepted):** on the free tier, Google may use prompts
  and outputs to improve its products, and human reviewers may read them.
- **Platform risk (accepted):** Google can lower or remove free limits at any time (it did
  so in December 2025). Analysis then stops until the owner decides otherwise.
- **Billing rule:** the Google Cloud project has **no billing account**. Without one,
  Google cannot charge: over a limit it refuses (HTTP 429). The code cannot check billing;
  the owner checks it in the Google Cloud console. Linking a billing account, or using any
  model or feature outside the free tier, requires a new decision recorded in this section.
- **Limits** (the same numbers in `CLAUDE.md`, this section and `.env.example`):
  - 20 requests per minute (Google: 30);
  - 10,000 input tokens per minute (Google: 16,000);
  - 1,000 requests per day (Google: 14,400; the owner's stricter rule, about 7%);
  - 3 answered requests per `news lab` test;
  - concurrency 8 articles in flight by default (`GEMINI_CONCURRENCY`).
  The per-minute and token limits are at least one third below Google's, as the owner
  requires. Settings can lower these limits; values above the ceilings are refused. The
  lab limit (3) and the concurrency ceiling (8) are constants in
  `app/analysis/budget.py` (`CEILINGS`). The per-minute window is shared across
  processes through `api_calls`: before its first request, a client seeds its window
  from the last 60 seconds of `api_calls` rows of any process (requests and
  `est_input_tokens`), so a new run starts where the previous one left off. Input tokens are estimated
  before sending as UTF-8 bytes // 3 (+1).
- **Advice given and declined:** keep analysis local, or send only synthetic text and
  metadata.
- **Safeguards in code** (`app/analysis/budget.py`, `app/analysis/llm.py`,
  `app/analysis/pipeline.py`):
  - only `https://generativelanguage.googleapis.com` and only models on the free
    allowlist (`gemma-4-31b-it`). The request body is fixed in code: no tools, grounding,
    caching or other options;
  - parallel and resumable: articles are admitted one at a time under one lock, so
    parallelism never passes a limit. A "busy" answer pauses all workers together. Each
    result is committed at once in `article_analyses`, so a stop loses only the requests
    in flight, and the next run resumes. Unusable answers (cut off, invalid) are retried up
    to 3 times per article, each attempt stored with its raw answer;
  - each request is counted **before** it is sent, so a failure still counts. The day
    follows Google's reset (midnight Pacific time);
  - **the quota ledger is the database table `api_calls`** (`DatabaseLedger`,
    `app/analysis/ledger.py`). Each run writes a row to `analysis_runs`.
    `news gemini-usage` reads the ledger and shows use against our limits and Google's.
    The ledger **reserves before it sends**: in one transaction under
    `pg_advisory_xact_lock`, it counts today's sent requests and, below the daily limit,
    inserts an `api_calls` row, committed before the request goes out. Two processes can
    never both take the last slot. The row is inserted as a `'failed'` placeholder (an
    outcome that counts toward the limit) and updated to the real outcome afterwards. A
    crash between the two leaves a `'failed'` row: the ledger can over-count, never
    under-count. While a request is in flight, other processes see it as `'failed'`; only
    the reserving process reports it as pending. The daily cap is counted **per model**.
    Only one model is allowed, so this is the project-wide cap; widening the allowlist
    would first need a global count;
  - "high demand" errors (HTTP 503, also 500/502/504, timeouts, no network) are expected on
    the free tier and never break a run (owner decision): the same request, same model, is
    tried again after 10, 20, 30, then every 60 seconds until Google answers. Every attempt
    counts toward the daily limit, which stays the absolute stop. Busy refusals do not use
    the per-test limit (3 answered requests), since Google does not count them either. HTTP
    429 (quota) or any other error stops sending, and the run still writes its report. An
    article too long for the token limit (about 0.5% of articles) is skipped without being
    sent;
  - the API key lives only in `.env` (see `docs/decisions/security.md`).
- **Nothing else in 5.4 changes:** article text and analyses are never served, exported,
  published or committed.
- **Reversal plan:**
  1. Stop sending: clear `GEMINI_API_KEY` in `.env`. The client then refuses to run.
     Record the decision here.
  2. Return to a local model, or link a billing account: a new owner decision, recorded
     here first.
  3. Stored outputs: delete the affected rows from `article_analyses` (and related rows)
     only when the owner decides.

### 5.6 Owner decision: database on Neon, daily ingest on GitHub Actions (2026-11-25)

**Decision.** The owner wants the data reachable while the Mac is off. The whole
database, including `article_bodies`, moves to **Neon** (serverless PostgreSQL, free
plan, region AWS Europe/Frankfurt). A scheduled GitHub Actions workflow runs
`news ingest` **once a day**. The owner accepted, explicitly, these exceptions to
earlier rules:

- **Article text leaves the Mac.** It is stored privately on Neon's servers (AWS). Still
  never served, exported, published or committed: everything else in 5.4 holds.
- **A free tier is a dependency.** Neon's free plan can change and a project can be
  suspended. The local Docker database stays usable as a fallback (see reversal).
- **PostgreSQL is reachable from the internet** (Neon's endpoint), protected by TLS
  (`sslmode=require`) and passwords. This is the only exception: the local container is
  still bound to localhost.
- **Ingestion runs on a schedule, on a GitHub-hosted runner.** It replaces "run by hand"
  (manual runs still work). Gemini analysis is **not** scheduled: it stays a manual
  command.

**How it is constrained.**
- `.github/workflows/ingest.yml`: daily cron and manual dispatch; never on pull requests;
  `contents: read`; one run at a time (workflow concurrency, then the advisory lock);
  runs only `news ingest`, never tests, migrations or analysis; refuses a URL without
  `sslmode=require` and Neon's `-pooler` endpoint (session advisory locks need a direct
  session).
- Its only secret is `DATABASE_URL` (in the `production` environment), the same owner
  URL as on the Mac. `ci.yml` still has no secrets and never touches real data.
- **One connection string everywhere (owner decision, 2026-11-25):** the Mac, the API and
  GitHub all use `DATABASE_URL`, to keep the setup simple. Accepted trade-off: the
  separate least-privilege logins (`news_ingest_login`, `news_api_login`) are not used,
  so a leaked secret gives full access to the database. What still protects article
  text: the API's connections are read-only and no route serves `article_bodies` (tests
  enforce both); the roles scripts stay in the repo as an optional extra.
- Migrations are run by the owner, from the Mac, against Neon. The ingest refuses to run
  while the database is behind the code's Alembic head.
- **Freshness:** a daily walk to the checkpoint covers the last day and any missed days
  within the RSS depth (section 4). A skipped GitHub schedule is caught up by the next
  run. GitHub disables schedules after 60 days without repository activity: re-enable
  it in the Actions tab.
- **Backups:** `news backup` and `news restore-test` work against Neon unchanged: the
  client tools run in a throwaway `postgres:18-alpine` container (Neon runs Postgres 18),
  and dumps still go to `~/Backups/news` on the Mac. The scratch restore database is
  created and dropped on Neon.
- **The local Docker database is for tests only** (`TEST_DATABASE_URL`):
  `tests/conftest.py` refuses a Neon URL. Its old data was deleted by the owner's choice
  on 2026-11-25 (a verified dump from that morning is kept in `~/Backups/news`).

**Reversal plan.**
1. Disable the workflow in the Actions tab and delete the `DATABASE_URL` secret.
2. Dump Neon (`news backup`), restore into the local container, re-run the roles
   scripts, point `DATABASE_URL` back at `localhost:5433`.
3. Delete the Neon project (this removes the stored article text there). Record it here.

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
  - the schema owner is the database owner (`neondb_owner` on Neon; `news` in the local
    test container), which runs migrations;
  - `news_ingest` reads and writes data, cannot change the schema, and can only read
    `alembic_version` (the ingest checks the migration head);
  - `news_api` has SELECT only, on an explicit allowlist: `articles`, `feeds`, `sources`
    and the `analysis_briefings` view. Nothing else, not even future tables.

  Both are NOLOGIN group roles. The login roles `news_api_login` and `news_ingest_login`
  inherit them; their passwords are set by `scripts/db_login_passwords.sql` from psql
  variables, never from a file. `API_DATABASE_URL` can point the API at
  `news_api_login`. **Since 2026-11-25 these logins are optional and unused by default**
  (section 5.6: one connection string everywhere).
  At startup the API runs `assert_least_privilege` (`app/db/session.py`): if its login is
  a superuser or can SELECT `article_bodies`, it refuses to start in production and logs
  a warning elsewhere.
- **Retention:** articles are kept indefinitely (metadata). `ingestion_runs` for 180 days.
  Resolved `discovered_urls` for 30 days. Bodies only as their license allows.
- **Deliberately not stored:** raw feed payloads, because they contain full text.

## 7. API (milestone 7)

- **Endpoints:** `GET /api/v1/sources`, `/feeds`, `/articles`, `/articles/{id}`,
  `/briefings` (keyset-paginated briefing summaries: headline, report type, event status
  and date, top 3 places and actors, `review_required`) and `/articles/{id}/briefing`
  (the full briefing, article metadata and `source_url`; 404 if none). They only read
  stored data; nothing fetches publishers on a request.
- **Briefings, not analyses:** at save time the pipeline also stores
  `article_analyses.briefing`, built by
  `make_briefing(cleaned, unverified_paths, paragraphs=...)` (`app/analysis/briefing.py`),
  in this order:
  1. **Quarantine on the cleaned analysis.** The pipeline verifies the cleaned analysis,
     the same object the briefing is made from, so indexes cannot shift between
     verification and removal. Every unverified item is dropped (an unverified event
     date becomes null). A path that does not resolve raises instead of being ignored.
  2. Every `evidence` key is removed, at any depth.
  3. **Originality filter.** Any remaining string that shares a run of **8 or more
     consecutive tokens** (`MIN_COPIED_RUN`, after normalization) with an article
     paragraph is removed: its enclosing list item is dropped, or a scalar field outside a
     list is set to null. Proper-name fields (`name_as_written`, `full_name`,
     `attributed_to`) are exempt.

  Added keys: `review_required`, `removed_items` (by quarantine and originality),
  `copied_items` (how many removals the originality filter made), `copied_paths`, and
  `unverified_fields`: served fields that have no evidence at all (headline, main event,
  how, context, ...), which code cannot verify and readers must treat as unverified.
  Only model-written fields remain.
- **Evidence rules:** a quote is `too_short` (never verified) unless it has at least 4
  tokens or at least 20 characters, and it must match whole words. Attributions are
  checked too: a claim from an anonymous source without attribution, or an attribution
  naming someone the article never names, is flagged for review
  (`app/analysis/cleanup.py`).
- **Attempt history:** `article_analyses.attempt_history` (JSONB list) gets one entry per
  attempt. Entries hold no raw answer and no article text.
- **Migrations of round 3:** `de01c28240f2` (least privilege: the briefings partial
  index, foreign-key indexes, a GIN index on `articles.categories`, non-negative CHECKs,
  an identity column); `7c3e9a1f5b20` (`api_calls.est_input_tokens` and `error`, for
  cross-process pacing); `aa372fadc9eb` (adds `attempt_history`, then recomputes every
  answered analysis's evidence statuses, `unverified_paths`, `status` and `briefing` with
  the rules above, using a frozen copy of the transform; an analysis whose article has no
  body any more gets a NULL briefing, so it is not served).
- **Effect on the real data (honest note):** on the stored analyses the originality
  filter removed between 1 and 13 copied items per article: of 83 items removed in total
  across 10 articles, 74 were copies. Briefings are now much thinner. That is a quality
  and coverage tradeoff to watch: originality is an owner rule, but the prompt may need
  to ask for more original wording so that less is dropped. Only `succeeded` and `needs_review` rows are served. When an article has several
  analyzer versions, the most recently updated briefing is served. Quotes, raw output,
  the raw `analysis` and body text are never served. The response schemas
  (`app/schemas/analysis.py`) list the servable fields explicitly and ignore any other key.
- **View-based privileges:** the `analysis_briefings` view
  (`article_id, analyzer_version, status, briefing, updated_at`; migration
  `9420376cc156`) is the only analysis object granted to `news_api`. A view runs with its
  owner's privileges, so `news_api` has `SELECT` on the view and still no right at all
  on `article_analyses`, `article_bodies`, `api_calls` or `analysis_runs`
  (tests/integration/test_database_security.py).
- **Data migration `14389f50095c`:** analyses stored before the quarantine rule were
  `succeeded` even with unverified quotes. It reclassified those rows (3 articles, 6
  quotes, 2026-11-24) to `needs_review` from their stored evidence statuses, set
  `unverified_paths` and rebuilt their briefings. No article text is read and no model is
  called. Its downgrade is a deliberate no-op: reverting would break the CHECK constraint
  `ck_article_analyses_succeeded_fully_verified`.
- **Pagination:** keyset on `(published_at DESC, id DESC)` with an opaque cursor; `limit`
  is capped at 100. Never `OFFSET`. `/briefings` uses the same cursor as `/articles`: it
  walks articles in keyset order and joins each one with a `LATERAL` subquery to its
  latest briefing, so a page costs about `limit` index probes instead of sorting every
  briefing.
- **Error envelope:** every error (400, 404, 405, 422, 500, 503) has the shape
  `{"error": {"code", "message", "details"?}, "request_id"}` (`app/core/errors.py`).
  Validation details carry only the location, message and type, never the rejected
  input. A statement timeout answers 503 `query_timeout`; a database outage, 503
  `database_unavailable`.
- **Input limits:** `cursor` and `q` at most 200 characters, `source` 64, `category` 100,
  `language` 16, with NUL bytes refused; ids between 1 and the bigint maximum; `limit`
  1 to 100. API connections set `statement_timeout` to 5 s.
- **Briefing screen (`/ui`):** static HTML, CSS and JavaScript in `app/web/static/`,
  mounted by `app/main.py`, calling only `/api/v1`. `UICSPMiddleware` adds to every `/ui`
  response a strict Content Security Policy (`default-src 'self'`, no inline script,
  `object-src 'none'`, `base-uri 'none'`, `form-action 'none'`,
  `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer` and `Cache-Control: no-cache` (revalidated through the
  ETag, so an updated UI is never hidden behind cached modules). All data enters the DOM
  through `textContent` or attributes, never as HTML (`dom.js`).
- **Filters:** source, category, language, date range, and `q` (Postgres full-text
  search, best effort).
- **Caching:** `ETag` and `Cache-Control` headers.
- **Access:** connects as the SELECT-only `news_api` role. Local only. API keys and rate
  limiting are added only if remote access is ever enabled.

## 8. Backups

- **`uv run news backup`** (`app/ops/backup.py`):
  - It takes the ingest advisory lock, so a dump never captures half a run; it is refused
    while an ingest runs.
  - **Snapshot-consistent:** it opens a `REPEATABLE READ, READ ONLY` transaction,
    exports its snapshot (`pg_export_snapshot()`), records exact per-table row counts and
    the Alembic revision inside it, then streams `pg_dump -Fc --snapshot=...`, so the counts and the dump describe the
    same data. For Neon the tools run in a throwaway `postgres:18-alpine` container (the
    server's major version); the password and TLS settings travel in `PG*` environment
    variables, never on a command line.
  - It writes `news-<UTC stamp>.dump.partial` and renames it atomically. A JSON manifest
    holds the size, sha256, revision, counts and `verified_at`.
  - Files are mode 600 in a 700 directory, `~/Backups/news/`: outside the repo and outside
    iCloud. The newest 14 are kept. Time Machine provides a second copy.
- **`uv run news restore-test`:**
  - It verifies the newest dump's sha256, restores it into a scratch database
    `<db>_restore_check` (`--no-owner --no-privileges --exit-on-error`), and compares every
    table's row count with the manifest.
  - On success it sets `verified_at`. The scratch database is always dropped; the live
    database is only read.
  - **A backup only counts once a restore test has passed.**
- **Reminder:** `news ingest` warns when there is no *verified* backup newer than
  `BACKUP_MAX_AGE_DAYS` (7).
- **Restoring for real** (disaster recovery) is deliberately manual (README, "Restore a
  backup"): `pg_restore --no-owner --no-privileges --clean --if-exists` into Neon,
  then **re-run `scripts/db_roles.sql`** (and `scripts/db_login_passwords.sql` if the
  login roles are new): dumps are restored without privileges.

## 9. CI and GitHub

- `ci.yml` stays CI-only: ruff, mypy, `alembic upgrade head` + `alembic check`, and
  pytest against a throwaway Postgres service. It has no secrets. No self-hosted runner.
- `ingest.yml` is the daily ingest against Neon (section 5.6). Its single secret is
  `DATABASE_URL`; `tests/test_workflows.py` guards both workflows.
  - The Postgres service image is pinned by digest (`postgres:18-alpine@sha256:...`).
  - Coverage is measured with branch coverage and must stay at least 95%
    (`fail_under` in `pyproject.toml`). `pytest-randomly` shuffles test order.
  - CI runs `pre-commit run --all-files` (ruff, file checks, gitleaks, mypy), so a
    skipped local hook is still caught.
  - **Hygiene gate:** `scripts/check_repo_hygiene.py` (standard library, no network)
    fails on secret patterns, forbidden file names (dumps, `.env`, JSONL exports),
    article-quote markers, article-text JSON keys (`"quote"`, `"body_text"`,
    `"raw_output"`) in non-test JSON files, and deny-listed geography words. That list
    holds SHA-256 hashes only, so the words never appear in the repo.
- Migrations are applied locally with `uv run alembic upgrade head`. `news ingest`
  refuses to run when the database is behind.
- One GitHub secret, `DATABASE_URL` (environment `production`), used by `ingest.yml` only.
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
| 7 ✅ | Read API | `app/api/v1/catalog.py`, `app/api/caching.py`, `app/schemas/catalog.py`, `app/db/session.py` | Sources, feeds, articles (keyset, filters, FTS), detail; weak ETag + 304; read-only connections (`default_transaction_read_only`), optional `API_DATABASE_URL` in `news_api` | Every article exactly once across pages; stable under inserts; filters and search; 400/422 validation; no body ever served; 304 revalidation; API engine cannot write |
| 8 ✅ | Backups | `app/ops/backup.py`, `news backup`, `news restore-test` | Locked, atomic, checksummed dumps; retention; restore into a scratch DB; ingest reminder | Real round trip (local Docker); corrupted dump; row-count mismatch not marked verified; refused during ingest; pruning, partials, freshness, private files |
| 9 ✅ | Analysis framework | `app/analysis/*`, `news analyze`, `lab`, `gemini-usage`, `eval` | Gemma 4 31B on the Gemini API free tier (section 5.5): budgeted, resumable, evidence-checked; briefings with quarantine and the originality filter (section 7) | Budget ceilings, ledger reservation, retries, evidence and originality rules; the real API is never called from tests |
| 10 ✅ | Frontend | `app/web/static/`, mounted at `/ui` | Read-only briefing screen, dark and light themes, link-out and attribution, strict CSP | `tests/test_ui.py` |

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

## 12. Scalability plan

Everything stays on the owner's Mac at $0. Nothing here adds a paid service.

- **Analysis throughput (the real bottleneck).** Our limits are 20 requests per minute,
  10,000 input tokens per minute and 1,000 requests per day. A request carries about
  3,600 to 4,600 input tokens (measured, prompt v8 / schema s7), so the token limit, not
  the request limit, binds: about 2 requests per minute. Each answer also takes about 170
  to 230 s. For about 1,600 articles that is roughly 13 to 14 hours of sending, and at
  least 2 quota days (Pacific time). Busy refusals (503) and retries of unusable answers
  also count toward the daily limit (in one test, 18 of 27 requests were busy refusals),
  so plan on 3 to 4 days of runs. Runs are resumable, so the Mac being offline only
  delays the backlog. Once the backlog is done, a day's new articles from one source should fit well inside
  one day's cap.
- **What changes beyond one source.** Each source needs its own adapter, rights policy
  and `rights_basis` (an owner decision per source), robots.txt and Crawl-delay. Fetching
  stays sequential per host; ingest time grows linearly with sources, which is fine for a
  manual command. Analysis is shared: the daily cap is per project, so more sources means
  a longer backlog, not more throughput. If the volume outgrows the free tier, the options
  (analyse a subset, a local model, or billing) are owner decisions, not code changes.
- **When Postgres indexes or partitioning matter.** Existing indexes cover the hot paths:
  `articles(published_at DESC, id DESC)` for keyset pages, GIN on `search_tsv`,
  `article_analyses(article_id)`, `api_calls(quota_day, model)` and the pending
  `discovered_urls` partial index. At this scale (thousands to low millions of rows) one
  PostgreSQL instance needs nothing more. Add an index only when `EXPLAIN ANALYZE` shows a
  slow query (for example a filter on `source_id` once there are several sources, or on
  analysis status). Partitioning (by month on `published_at`) only becomes worth
  considering at tens of millions of articles, which a personal tool is not expected to
  reach. Retention (section 6) keeps `ingestion_runs` and resolved `discovered_urls`
  small.
