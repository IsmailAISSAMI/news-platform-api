import base64
import json
import logging
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import psycopg
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg.errors import QueryCanceled
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.v1.catalog import _decode_cursor
from app.core.config import Settings
from app.core.logging import JsonFormatter
from app.db import session as db_session
from app.main import create_app


def test_unhandled_exception_log_carries_request_id(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    with (
        caplog.at_level(logging.ERROR, logger="app.core.errors"),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        client.get("/boom", headers={"X-Request-ID": "req-42"})

    record = next(r for r in caplog.records if r.name == "app.core.errors")
    assert json.loads(JsonFormatter().format(record))["request_id"] == "req-42"


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
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Internal server error"},
        "request_id": "req-1",
    }
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Request-ID"] == "req-1"
    assert "secret" not in response.text


def _production(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"environment": "production", "api_database_url": settings.database_url}
    )


def test_docs_are_disabled_in_production(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def least_privilege_ok(engine: object, settings: object = None) -> None:
        return None  # the test login is the owner; the guard is tested separately

    monkeypatch.setattr(db_session, "assert_least_privilege", least_privilege_ok)
    prod = create_app(_production(settings))

    with TestClient(prod) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_production_refuses_to_start_without_api_database_url(settings: Settings) -> None:
    prod = create_app(settings.model_copy(update={"environment": "production"}))

    with pytest.raises(db_session.PrivilegeError, match="API_DATABASE_URL"), TestClient(prod):
        pass


def test_production_refuses_a_login_that_can_read_article_text(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def too_powerful(engine: object, settings: object = None) -> None:
        raise db_session.PrivilegeError("superuser")

    monkeypatch.setattr(db_session, "assert_least_privilege", too_powerful)
    prod = create_app(_production(settings))

    with pytest.raises(db_session.PrivilegeError), TestClient(prod):
        pass


def test_cors_is_only_enabled_for_configured_origins(settings: Settings) -> None:
    cors_app = create_app(settings.model_copy(update={"cors_origins": ["https://example.com"]}))

    with TestClient(cors_app) as client:
        allowed = client.get("/health/live", headers={"Origin": "https://example.com"})
        denied = client.get("/health/live", headers={"Origin": "https://evil.test"})

    assert allowed.headers["access-control-allow-origin"] == "https://example.com"
    assert "access-control-allow-origin" not in denied.headers


FORBIDDEN_EXPOSURE_NAMES = frozenset({"body_text", "body", "evidence", "raw_output", "raw"})
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "options", "head", "trace"})


def _property_names(node: object) -> set[str]:
    """Every property name found anywhere in a JSON schema fragment (including $defs)."""
    names: set[str] = set()
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.update(properties.keys())
        for value in node.values():
            names.update(_property_names(value))
    elif isinstance(node, list):
        for item in node:
            names.update(_property_names(item))
    return names


def test_v1_routes_are_get_only_and_never_expose_article_text(settings: Settings) -> None:
    # FastAPI wraps included routers, so the OpenAPI document is the complete, flattened
    # view of every route and the response schemas it declares.
    spec = create_app(settings).openapi()
    v1_paths = {path: item for path, item in spec["paths"].items() if path.startswith("/api/v1")}

    assert v1_paths, "expected business routes under /api/v1"
    for path, item in v1_paths.items():
        methods = {m for m in item if m in HTTP_METHODS}
        assert methods == {"get"}, f"{path} allows {sorted(methods)}"
        path_params = {p.strip("{}") for p in path.split("/") if p.startswith("{")}
        assert not path_params & FORBIDDEN_EXPOSURE_NAMES, path

    leaked = _property_names(spec["components"]["schemas"]) & FORBIDDEN_EXPOSURE_NAMES
    assert not leaked, f"/api/v1 response schemas expose {sorted(leaked)}"


def test_unknown_route_and_wrong_method_use_the_envelope(client: TestClient) -> None:
    missing = client.get("/api/v1/nope", headers={"X-Request-ID": "r-404"})
    wrong = client.post("/api/v1/articles", headers={"X-Request-ID": "r-405"})

    assert missing.status_code == 404
    assert missing.json() == {
        "error": {"code": "not_found", "message": "Not Found"},
        "request_id": "r-404",
    }
    assert wrong.status_code == 405
    assert wrong.json()["error"]["code"] == "method_not_allowed"
    assert "GET" in wrong.headers["allow"]


def test_validation_errors_use_the_envelope_without_echoing_input(client: TestClient) -> None:
    response = client.get("/api/v1/articles", params={"limit": "SECRET-INPUT"})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"][0]["loc"] == ["query", "limit"]
    assert "SECRET-INPUT" not in response.text


@pytest.mark.parametrize(
    "params",
    [
        {"source": "a\x00b"},
        {"category": "a\x00b"},
        {"language": "e\x00"},
        {"q": "ab\x00cd"},
        {"source": "s" * 65},
        {"category": "c" * 101},
        {"language": "l" * 17},
        {"published_after": "2026-11-23T12:00:00"},  # naive: no offset
        {"published_before": "2026-11-23T12:00:00"},
        {"published_after": "2026-11-23T12:00:00Z", "published_before": "2026-11-23T12:00:00Z"},
        {"published_after": "2026-11-24T00:00:00Z", "published_before": "2026-11-23T00:00:00Z"},
    ],
)
def test_bad_text_and_date_filters_are_422(client: TestClient, params: dict[str, str]) -> None:
    response = client.get("/api/v1/articles", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("article_id", ["0", "9223372036854775808", "-1"])
@pytest.mark.parametrize("suffix", ["", "/briefing"])
def test_article_ids_outside_bigint_are_422(
    client: TestClient, article_id: str, suffix: str
) -> None:
    response = client.get(f"/api/v1/articles/{article_id}{suffix}")

    assert response.status_code == 422


def test_security_headers_on_every_response(client: TestClient) -> None:
    for response in (client.get("/health/live"), client.get("/api/v1/nope")):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"


def test_head_is_answered_like_get_without_a_body(client: TestClient) -> None:
    get = client.get("/health/live")
    head = client.head("/health/live")

    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == get.headers["content-length"]


def test_cors_allows_get_only(settings: Settings) -> None:
    cors_app = create_app(settings.model_copy(update={"cors_origins": ["https://example.com"]}))

    with TestClient(cors_app) as client:
        preflight = client.options(
            "/api/v1/articles",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "DELETE",
            },
        )

    assert preflight.status_code == 400
    assert "DELETE" not in preflight.headers.get("access-control-allow-methods", "")


def test_description_describes_this_project(settings: Settings) -> None:
    assert "Enterprise" not in (create_app(settings).description or "")


def _raw_cursor(data: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "data",
    [
        {"p": "2026-11-23T12:00:00+00:00", "i": True},
        {"p": "2026-11-23T12:00:00+00:00", "i": 1.0},
        {"p": "2026-11-23T12:00:00+00:00", "i": 2**63},
        {"p": "2026-11-23T12:00:00+00:00", "i": 0},
        {"p": "2026-11-23T12:00:00+00:00", "i": "1"},
        {"p": 5, "i": 1},
        [1, 2],
    ],
)
def test_cursor_ids_must_be_bigint_ints(data: object) -> None:
    with pytest.raises(HTTPException) as caught:
        _decode_cursor(_raw_cursor(data))

    assert caught.value.status_code == 400


def test_cursor_accepts_the_largest_bigint() -> None:
    assert _decode_cursor(_raw_cursor({"p": "2026-11-23T12:00:00+00:00", "i": 2**63 - 1}))[1] == (
        2**63 - 1
    )


@pytest.mark.parametrize(
    ("orig", "code"),
    [
        (QueryCanceled("canceling statement due to statement timeout"), "query_timeout"),
        (psycopg.OperationalError("connection lost"), "database_unavailable"),
    ],
)
def test_database_errors_map_to_503(app: FastAPI, orig: Exception, code: str) -> None:
    async def broken_db() -> AsyncIterator[AsyncSession]:
        session = AsyncMock(spec=AsyncSession)
        session.execute.side_effect = OperationalError("SELECT 1", {}, orig)
        yield session

    app.dependency_overrides[get_db] = broken_db

    with TestClient(app) as client:
        response = client.get("/api/v1/sources")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == code
