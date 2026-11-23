# News Platform API

Collect news articles from publishers into your own database, then browse and search them
through a simple web API. Everything runs on your own computer, for free.

The first supported publisher is **[Hespress English](https://en.hespress.com)**.

> **What it stores:** article *information* only (title, link, date, author, categories).
> It never copies article text, and every article links back to the original. See
> [Respecting publishers](#respecting-publishers).

---

## What you need

Install these three tools first (all free):

| Tool | What it's for | Install |
| --- | --- | --- |
| **Git** | Download the project | [git-scm.com/downloads](https://git-scm.com/downloads) |
| **Docker Desktop** | Runs the database and the API | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) |
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

**2. Create your settings file with a secure password**

```bash
cp .env.example .env
PW=$(openssl rand -hex 24) && sed -i.bak "s/change-me/$PW/g" .env && rm .env.bak
```

This copies the example settings and replaces every `change-me` with a random password.
The `.env` file is private: it is never uploaded to GitHub.

**3. Install the Python tools**

```bash
uv sync
```

**4. Start the database and the API**

```bash
docker compose up -d --build
```

This starts PostgreSQL, prepares the database, and starts the API. The first time takes a
few minutes while Docker downloads what it needs.

**5. Check that it works**

Open **<http://localhost:8000/health/ready>** in your browser. You should see:

```json
{"status": "ready"}
```

✅ Setup is done.

---

## Collect articles

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

This **replaces** the current database contents with the backup:

```bash
ls ~/Backups/news/      # pick a .dump file
docker compose exec -T db pg_restore -U news -d news --clean --if-exists < ~/Backups/news/<file>.dump
```

</details>

---

## Everyday commands

| I want to… | Command |
| --- | --- |
| Start everything | `docker compose up -d` |
| Stop everything (your data is kept) | `docker compose stop` |
| Collect new articles | `uv run news ingest` |
| Collect with progress details | `uv run news ingest -v` |
| Back up and verify | `uv run news backup && uv run news restore-test` |
| See all options | `uv run news --help` |
| Update to the latest version | `git pull && uv sync && docker compose up -d --build` |

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `Not ready: PostgreSQL is not reachable` | Start Docker Desktop, then run `docker compose up -d`. |
| `Not ready: The database is at migration …` | You updated the code. Run `docker compose up -d --build` (or `uv run alembic upgrade head`). |
| `port is already allocated` for **8000** | Another program uses port 8000. Run `API_PORT=8001 docker compose up -d` and use `http://localhost:8001`. |
| `port is already allocated` for **5433** | Another program uses port 5433. Stop it, or change `5433` in `docker-compose.yml` **and** in `DATABASE_URL` in `.env`. |
| `Not started: another news ingest is already running` | Wait for the other run to finish (only one runs at a time). |
| `password authentication failed` | The password in `.env` doesn't match the database. If you have no data yet: `docker compose down -v` (⚠️ deletes the database), then `docker compose up -d --build`. |
| A feed shows `FAILED` in the report | Usually a temporary network or site problem. Run again later. After 20 failures in a row a feed is paused; see `/api/v1/feeds`. |
| Strange import errors on macOS | If the project is inside an iCloud-synced folder (Desktop or Documents), iCloud can damage the Python environment. Move the project elsewhere, e.g. `~/code`, then run `uv sync`. |

---

## Respecting publishers

This project is designed to be a good citizen:

- **Information only.** For Hespress it stores titles, links, dates, authors and categories.
  It never stores or shows article text, because the publisher's terms reserve copying and
  republication of its texts.
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

**How it fits together:** a manually run command (`news ingest`) collects articles into a
local PostgreSQL; a read-only FastAPI app serves them. The full design, including how
catch-up and rights handling work, is in
[docs/architecture.md](docs/architecture.md). Conventions for AI-assisted work are in
[CLAUDE.md](CLAUDE.md).

```
app/
  cli.py          the `news` command (ingest, backup, restore-test)
  main.py         FastAPI app
  api/            HTTP endpoints (health checks, /api/v1)
  ingestion/      polite fetching, robots.txt, parsing, rights rules, catch-up walk
  ops/            backups and restore verification
  models/         database tables
  schemas/        API response shapes
alembic/          database migrations
scripts/          database roles (optional extra security)
tests/            automated tests
docs/             architecture and design decisions
```

**Run the checks** (the same ones GitHub runs on every change):

```bash
uv run pre-commit install          # once: run checks automatically before each commit
uv run ruff check . && uv run ruff format --check .
uv run mypy app tests alembic
uv run pytest                      # needs the database running
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

**Optional extra security:** `scripts/db_roles.sql` creates restricted database roles. The
API is read-only even without them; see the comments in that file.

```bash
docker compose exec -T db psql -U news -d news -v ON_ERROR_STOP=1 < scripts/db_roles.sql
```

**Command exit codes** (useful in scripts): `0` ok · `1` something failed · `2` database
not ready · `3` already running · `130` interrupted.

</details>
