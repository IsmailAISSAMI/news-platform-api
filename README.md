# News API

FastAPI backend for the news platform (Python 3.13, SQLAlchemy 2, Alembic, PostgreSQL).

## Setup

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
pre-commit install
```

## Run

```bash
uvicorn app.main:app --reload      # API on http://localhost:8000 (docs at /docs)
docker compose up --build          # API + PostgreSQL
```

## Quality checks

```bash
ruff check . && ruff format --check .
mypy app tests
pytest
```

## Migrations

```bash
alembic revision --autogenerate -m "message"
alembic upgrade head
```

## Layout

```
app/
  main.py        app factory
  api/           routers (v1/)
  core/          settings
  db/            engine, session, declarative base
alembic/         migrations
tests/
```
