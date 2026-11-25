# News Platform API

Collect news articles from publishers into your own database, then browse and search them
through a simple web API. The database is a free [Neon](https://neon.com) PostgreSQL, so
the data stays reachable while your computer is off; a GitHub workflow collects new
articles once a day.

The first supported publisher is **[Hespress English](https://en.hespress.com)**.

> **What it stores:** article information (title, link, date, author, categories), plus,
> for sources configured for it, the article text **kept privately in your own database**
> for analysis. The API never shows article text: every article links back to the original.
> See [Respecting publishers](#respecting-publishers).

---

## What you need

Install these three tools first (all free):

| Tool | What it's for | Install |
| --- | --- | --- |
| **Git** | Download the project | [git-scm.com/downloads](https://git-scm.com/downloads) |
| **Docker Desktop** | Runs the test database, the backup tools and (optionally) the API | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) |
| **A Neon account** | Hosts the database (free plan) | [neon.com](https://neon.com) |
| **uv** | Runs the project's Python tools (installs Python for you) | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |

Then **start Docker Desktop** and leave it running.

---

## Set up (once, about 5 minutes)

Run these commands in a terminal, one block at a time.

**1. Download the project**

```bash
git clone https://github.com/IsmailAISSAMI/news-platform-api.git
cd news-platform-api
```

**2. Create your settings file**

```bash
cp .env.example .env
PW=$(openssl rand -hex 24) && sed -i.bak "s/change-me/$PW/g" .env && rm .env.bak
```

This copies the example settings and replaces every `change-me` with a random password
(used by the local test database). The `.env` file is private: it is never uploaded to
GitHub.

**3. Install the Python tools**

```bash
uv sync
```

**4. Create the Neon database**

In the [Neon console](https://console.neon.tech): new project, region **AWS Europe
(Frankfurt)**, Postgres **18**, only the *Postgres database* service. Then *Connect*,
turn **Connection pooling off** (the address must not contain `-pooler`), and copy the
connection string into `.env` as is:

```bash
DATABASE_URL=postgresql://neondb_owner:change-me@ep-xxx.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require
```

Never paste a connection string into a chat, an issue or a file in this repo.

**5. Create the tables**

```bash
uv run alembic upgrade head
```

**6. Start the local test database** (used by the tests only, never by the app):

```bash
docker compose up -d db
```

and point the tests at it in `.env` (the password is `POSTGRES_PASSWORD` from `.env`):

```bash
TEST_DATABASE_URL=postgresql+psycopg://news:change-me@localhost:5433/news
```

**7. Start the API and check it**

```bash
uv run uvicorn app.main:app --port 8000
```

Open **<http://localhost:8000/health/ready>** in your browser. You should see:

```json
{"status": "ready"}
```

**8. Turn on the daily collection** (GitHub): repo → *Settings* → *Environments* → new
environment `production` → *Add secret* `DATABASE_URL`: the same value as
`DATABASE_URL` in your `.env`. Then *Actions* → *Daily ingest* →
*Run workflow* once. After that it runs every day at 22:47 UTC (23:47 in Morocco); each run stores only
the articles published since the previous successful run, and a failed run emails you.

✅ Setup is done.

---

## Collect articles

GitHub collects new articles every day (setup step 8). To collect now from your computer:

```bash
uv run news ingest -v
```

**The first run** collects the last **30 days** of articles (about 7 minutes). To start
with more or less history, choose now, because this only applies to the very first run:

```bash
uv run news ingest --since-days 7 -v     # one week, about 2 minutes
uv run news ingest --since-days 90 -v    # three months, about 20 minutes
```

**After that,** run `uv run news ingest` whenever you like: daily, weekly, or after weeks
away. It always collects exactly what was published since your last run, and nothing
twice. There is no scheduler to set up.

Good to know:

- **You can stop it at any time** (Ctrl-C, closing the laptop, losing Wi-Fi). Nothing is
  lost: the next run picks up where this one stopped.
- **It is deliberately slow and polite:** it waits 5 seconds between requests, as the
  publisher asks.
- **At the end it prints a short report**, for example:

  ```
  Ingestion run #3: succeeded in 41.2s
    hespress-en    rss            +18 new, 0 updated, 0 skipped, 2 page(s) [checkpoint]
    hespress-en    news_sitemap   +0 new, 0 updated, 0 skipped, 1 page(s) [not_modified]
  Total: +18 new, 0 updated, 3 page(s)
  ```

**Fetch article text for articles you collected earlier** (once, if you collected articles
before text storage was enabled):

```bash
uv run news ingest --rewalk-days 90 -v   # re-reads the last 90 days, about 15 minutes
```

---

## Analyse articles with AI (optional)

`uv run news analyze` sends article text to Google's Gemini API (free tier only; see
[Respecting publishers](#respecting-publishers)). Setup step 2 replaced the
`GEMINI_API_KEY=change-me` placeholder with a random value, which is **not a key**:
before running `news analyze` (or `news lab`), put a real key from Google AI Studio in
`.env` as `GEMINI_API_KEY=...`. Keep billing off on the Google project. The command
stays within fixed limits (20 requests per minute, 1,000 per day) and can be stopped and
resumed; `uv run news gemini-usage` shows what was used.

---

## Browse and search articles

Open **<http://localhost:8000/docs>**. It is an interactive page where you can try every
request in your browser: click an endpoint, then **Try it out**, then **Execute**.

Or open these links directly:

| What | Link |
| --- | --- |
| Latest articles | <http://localhost:8000/api/v1/articles> |
| Search for "economy" | <http://localhost:8000/api/v1/articles?q=economy> |
| One category | <http://localhost:8000/api/v1/articles?category=Politics> |
| Since a date | <http://localhost:8000/api/v1/articles?published_after=2026-11-01T00:00:00Z> |
| 50 per page | <http://localhost:8000/api/v1/articles?limit=50> |
| Publishers | <http://localhost:8000/api/v1/sources> |
| Feed status | <http://localhost:8000/api/v1/feeds> |

**Next page:** each response ends with a `next_cursor` value. Add it to the address as
`&cursor=…` to get the following page. When `next_cursor` is `null`, you've reached the end.

The API is **read-only**: nothing you do through it can change or delete data.

---

## Briefing screen

A read-only briefing screen ships with the API (plain HTML, CSS and JavaScript in
`app/web/static/`; nothing is loaded from the internet). It has a **dark theme and a
light theme** and follows your system setting (light when your computer is set to
light mode, dark otherwise).

With the API running (setup step 7), open <http://localhost:8000/ui/>. The list shows the newest briefings; open one to
see what, when, where and who, the claims, and a link to the original article.

The screen reads `GET /api/v1/briefings` (newest first, paged with `next_cursor`) and
`GET /api/v1/articles/{id}/briefing`. A briefing never contains article text: evidence
quotes are removed, unverified items dropped (`review_required` says when that
happened), and any sentence that copies 8 or more consecutive words from the article is
removed too. Summary fields that cannot be checked against the article are listed in
`unverified_fields`. Because of the copy filter, briefings can be short. The page is served with a strict Content Security Policy and `no-cache`, and
it inserts data as text only, never as HTML.

## Back up your data

Do this after your first collection, then about once a week:

```bash
uv run news backup         # saves a copy to ~/Backups/news
uv run news restore-test   # proves that the copy can really be restored
```

- A backup only counts once `restore-test` says **Verified**.
- The newest 14 backups are kept; older ones are removed automatically.
- `news ingest` reminds you when your last verified backup is more than 7 days old.

<details>
<summary><b>Restore a backup</b> (only if you lost your data)</summary>

This **replaces** the database contents with the backup. Paste the connection string
(`DATABASE_URL` from `.env`) at the prompt:

```bash
ls ~/Backups/news/      # pick a .dump file
read -rs NEON_URL
docker run --rm -i postgres:18-alpine pg_restore --no-owner --no-privileges \
  --clean --if-exists -d "$NEON_URL" < ~/Backups/news/<file>.dump
unset NEON_URL
```

</details>

**After each new migration** (when you update the code): run `uv run alembic upgrade head`
before the next daily collection. The
collection refuses to run while the database is behind the code.

---

## Everyday commands

| I want to… | Command |
| --- | --- |
| Start the API | `uv run uvicorn app.main:app --port 8000` (or `docker compose up -d api`) |
| Start the test database | `docker compose up -d db` |
| Collect new articles | `uv run news ingest` |
| Collect with progress details | `uv run news ingest -v` |
| Back up and verify | `uv run news backup && uv run news restore-test` |
| Analyse articles with AI (needs a real `GEMINI_API_KEY`) | `uv run news analyze` |
| Try the analysis on a few articles (max 3 answered requests) | `uv run news lab <ids...>` |
| See AI API use against the limits | `uv run news gemini-usage` |
| Export a sample to label (to `~/news-eval/gold`) | `uv run news eval export` |
| Score the labelled sample | `uv run news eval score ~/news-eval/gold/<dir>/labels.csv` |
| See all options | `uv run news --help` |
| Update to the latest version | `git pull && uv sync && uv run alembic upgrade head` |

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `Not ready: PostgreSQL is not reachable` | Check your internet connection and that `DATABASE_URL` in `.env` is Neon's connection string. Neon wakes an idle database in a few seconds: try again. |
| `Not ready: The database is at migration …` | You updated the code. Run `uv run alembic upgrade head`. |
| `port is already allocated` for **8000** | Another program uses port 8000. Use `--port 8001` (or `API_PORT=8001 docker compose up -d api`). |
| Tests say `DATABASE_URL points at Neon` | Set `TEST_DATABASE_URL` in `.env` (setup step 6). Tests never run on Neon. |
| `Not started: another news ingest is already running` | Wait for the other run to finish (only one runs at a time). |
| `password authentication failed` | The password in `.env` doesn't match. For Neon, copy the connection string again (or *Reset password* in the console). |
| The daily collection stopped running | GitHub pauses scheduled workflows after 60 days without repository activity. Re-enable it in the *Actions* tab. |
| A feed shows `FAILED` in the report | Usually a temporary network or site problem. Run again later. After 20 failures in a row a feed is paused; see `/api/v1/feeds`. |
| Strange import errors on macOS | If the project is inside an iCloud-synced folder (Desktop or Documents), iCloud can damage the Python environment. Move the project elsewhere, e.g. `~/code`, then run `uv sync`. |

---

## Respecting publishers

This project is designed to be a good citizen:

- **Article text stays private.** For Hespress, full text is stored only in your private
  Neon database, for non-commercial educational analysis: the owner's decision, recorded
  in the database and in `docs/architecture.md` sections 5.4 and 5.6.
  The API never shows it, and nothing is exported or published. The one exception: AI
  analysis sends article text to Google's Gemini API (free tier); see
  `docs/architecture.md` section 5.5. Hespress's terms reserve
  copying of its texts, and no permission was obtained, so **if you reuse this project,
  that decision is yours to make again**. To store information only, set the source's
  `rights_policy` to `metadata_only`.
- **Always links back.** Every article in the API points to the original page and names
  its publisher.
- **Polite collection.** It follows each site's `robots.txt`, waits 5 seconds between
  requests, identifies itself with a contact link, and only asks for what changed.

The project is for personal research. If you plan to publish anything or use it
commercially, check the publishers' terms and get legal advice first. This README is not
legal advice.

---

## For developers

<details>
<summary><b>Project layout, tests and contributing</b></summary>

**How it fits together:** a command (`news ingest`), run daily by GitHub Actions or by hand,
collects articles into PostgreSQL on Neon; a read-only FastAPI app serves them. The full design, including how
catch-up and rights handling work, is in
[docs/architecture.md](docs/architecture.md). Conventions for AI-assisted work are in
[CLAUDE.md](CLAUDE.md).

```
app/
  cli.py          the `news` command (ingest, analyze, lab, eval, gemini-usage, backup, ...)
  main.py         FastAPI app (and the /ui screen)
  api/            HTTP endpoints (health checks, /api/v1 catalog and briefings)
  analysis/       AI analysis: prompt, checks, cleanup, briefings, quota ledger, evaluation
  ingestion/      polite fetching, robots.txt, parsing, rights rules, catch-up walk
  web/static/     the briefing screen (HTML, CSS, JavaScript)
  ops/            backups and restore verification
  models/         database tables
  schemas/        API response shapes
alembic/          database migrations
scripts/          database roles and login passwords
tests/            automated tests
docs/             architecture and design decisions
```

**Run the checks** (the same ones GitHub runs on every change):

```bash
uv run pre-commit install          # once: run checks automatically before each commit
uv run ruff check . && uv run ruff format --check .
uv run mypy app tests alembic
uv run pytest                      # needs the local test database (docker compose up -d db)
                                   # and TEST_DATABASE_URL; never touches Neon
uv run pytest -m "not integration" # quick tests, no database needed
```

**Change the database schema:**

```bash
uv run alembic revision --autogenerate -m "describe the change"
uv run alembic upgrade head
uv run alembic check               # fails if the code and the migrations disagree
```

**Add a dependency:** `uv add <package>` (or `uv add --dev <package>`). Versions are locked
in `uv.lock`, which must be committed.

**All settings** live in `.env`; [.env.example](.env.example) lists every option with its
default.

**Optional database roles:** `scripts/db_roles.sql` and `scripts/db_login_passwords.sql`
can create restricted logins (an API login that can never read article text). They are
not used by default: everything connects with `DATABASE_URL`.

**Command exit codes** (useful in scripts): `0` ok · `1` something failed · `2` database
not ready · `3` already running · `130` interrupted.

</details>
