from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db


def test_live(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_ready_ok(app: FastAPI, client: TestClient) -> None:
    async def fake_db() -> AsyncIterator[AsyncSession]:
        yield AsyncMock(spec=AsyncSession)

    app.dependency_overrides[get_db] = fake_db

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_returns_503_when_database_is_down(app: FastAPI, client: TestClient) -> None:
    async def broken_db() -> AsyncIterator[AsyncSession]:
        session = AsyncMock(spec=AsyncSession)
        session.execute.side_effect = OperationalError("SELECT 1", {}, Exception("down"))
        yield session

    app.dependency_overrides[get_db] = broken_db

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}


def test_unversioned_legacy_route_is_gone(client: TestClient) -> None:
    assert client.get("/health").status_code == 404
