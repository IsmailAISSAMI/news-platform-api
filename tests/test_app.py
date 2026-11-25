import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.logging import JsonFormatter
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
