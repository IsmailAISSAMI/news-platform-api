import os
from pathlib import Path

# Fallback for fresh clones: only used when neither the environment nor a local .env
# provides DATABASE_URL. Must run before `app.main` is imported (it builds the app).
if "DATABASE_URL" not in os.environ and not Path(".env").exists():
    os.environ["DATABASE_URL"] = "postgresql+psycopg://news:change-me@localhost:5433/news"

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    # Reads the environment, then a local .env (same as the running app).
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # The context manager runs the app lifespan (engine start/stop).
    with TestClient(app) as test_client:
        yield test_client
