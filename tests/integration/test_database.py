import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_database_is_reachable(db_session: AsyncSession) -> None:
    result = await db_session.execute(text("SELECT 1"))

    assert result.scalar_one() == 1


def test_ready_endpoint_against_real_database(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
