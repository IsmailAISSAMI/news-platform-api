"""Contract: every endpoint's body matches the schema the OpenAPI document declares.

A small validator for the subset of JSON Schema FastAPI emits ($ref, anyOf, type,
properties, required, items, enum, const). It is strict about keys: a body key the
schema does not declare fails, so nothing is served without being documented.
Success bodies are checked against real (synthetic) rows in
tests/integration/test_api_briefings.py; the error-envelope checks here need no DB.
"""

from datetime import datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
    "array": (list,),
    "object": (dict,),
}


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"][name]
    return schema


def check(spec: dict[str, Any], schema: dict[str, Any], value: Any, where: str = "$") -> None:  # noqa: C901  # one branch per JSON Schema keyword
    schema = _resolve(spec, schema)
    if "anyOf" in schema:
        errors: list[str] = []
        for option in schema["anyOf"]:
            try:
                check(spec, option, value, where)
            except AssertionError as exc:
                errors.append(str(exc))
            else:
                return
        raise AssertionError(f"{where}: matches no anyOf option: {errors}")
    if "const" in schema:
        assert value == schema["const"], where
    if "enum" in schema:
        assert value in schema["enum"], where
    kind = schema.get("type")
    if kind is not None:
        expected = _TYPES[kind]
        ok = isinstance(value, expected) and not (kind in {"integer", "number"} and value is True)
        ok = ok and not (kind in {"integer", "number"} and isinstance(value, bool))
        assert ok, f"{where}: expected {kind}, got {type(value).__name__}"
        if kind == "string" and schema.get("format") == "date-time":
            datetime.fromisoformat(value)
    if isinstance(value, dict) and "properties" in schema:
        props: dict[str, Any] = schema["properties"]
        missing = set(schema.get("required", [])) - set(value)
        assert not missing, f"{where}: missing {sorted(missing)}"
        extra = set(value) - set(props)
        assert not extra, f"{where}: undeclared keys {sorted(extra)}"
        for key, item in value.items():
            check(spec, props[key], item, f"{where}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            check(spec, schema["items"], item, f"{where}[{i}]")


def response_schema(spec: dict[str, Any], path: str, status: int) -> dict[str, Any]:
    responses = spec["paths"][path]["get"]["responses"]
    declared: dict[str, Any] = responses[str(status)]["content"]["application/json"]["schema"]
    return declared


def test_validator_rejects_undeclared_keys_and_wrong_types(app: FastAPI) -> None:
    spec = app.openapi()
    schema = response_schema(spec, "/api/v1/articles/{article_id}", 404)
    good = {"error": {"code": "not_found", "message": "x"}, "request_id": "r"}
    check(spec, schema, good)
    with pytest.raises(AssertionError):
        check(spec, schema, good | {"detail": "x"})
    with pytest.raises(AssertionError):
        check(spec, schema, {"error": {"code": 1, "message": "x"}, "request_id": "r"})


A = "/api/v1/articles"
ONE = "/api/v1/articles/{article_id}"
BRIEFING = "/api/v1/articles/{article_id}/briefing"


@pytest.mark.parametrize(
    ("path", "route", "status"),
    [
        (f"{A}?cursor=bad", A, 400),
        (f"{A}?limit=0", A, 422),
        (f"{A}?source=a%00b", A, 422),
        (f"{A}/0", ONE, 422),
        (f"{A}/9223372036854775808", ONE, 422),
        (f"{A}/0/briefing", BRIEFING, 422),
        ("/api/v1/briefings?cursor=bad", "/api/v1/briefings", 400),
    ],
)
def test_error_bodies_match_the_declared_envelope(
    app: FastAPI, client: TestClient, path: str, route: str, status: int
) -> None:
    response = client.get(path, headers={"X-Request-ID": "req-c"})

    assert response.status_code == status
    spec = app.openapi()
    check(spec, response_schema(spec, route, status), response.json())
    assert response.json()["request_id"] == "req-c"
