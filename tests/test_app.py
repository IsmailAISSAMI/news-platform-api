from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_request_id_is_generated(client: TestClient) -> None:
    response = client.get("/health/live")

    assert len(response.headers["X-Request-ID"]) == 32


def test_valid_request_id_is_propagated(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "abc-123"})

    assert response.headers["X-Request-ID"] == "abc-123"


def test_invalid_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "bad id\twith spaces"})

    assert response.headers["X-Request-ID"] != "bad id\twith spaces"


def test_unhandled_exception_returns_consistent_500(app: FastAPI) -> None:
    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"X-Request-ID": "req-1"})

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "request_id": "req-1"}
    assert response.headers["X-Request-ID"] == "req-1"
    assert "secret" not in response.text


def test_docs_are_disabled_in_production(settings: Settings) -> None:
    prod = create_app(settings.model_copy(update={"environment": "production"}))

    with TestClient(prod) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_cors_is_only_enabled_for_configured_origins(settings: Settings) -> None:
    cors_app = create_app(settings.model_copy(update={"cors_origins": ["https://example.com"]}))

    with TestClient(cors_app) as client:
        allowed = client.get("/health/live", headers={"Origin": "https://example.com"})
        denied = client.get("/health/live", headers={"Origin": "https://evil.test"})

    assert allowed.headers["access-control-allow-origin"] == "https://example.com"
    assert "access-control-allow-origin" not in denied.headers
