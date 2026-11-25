"""One error envelope for every error response.

`{"error": {"code", "message", "details"?}, "request_id"}` for 400/404/405/422/500/503.
Validation details carry only the location, message and type: never the rejected input.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.errors import QueryCanceled
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.middleware import REQUEST_ID_HEADER

logger = logging.getLogger(__name__)

# Set on every response (also by SecurityHeadersMiddleware in app.main; repeated here
# because a 500 is answered outside the middleware stack).
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}

_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    422: "validation_error",
    500: "internal_error",
    503: "service_unavailable",
}


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class ErrorOut(BaseModel):
    error: ErrorBody
    request_id: str | None


def _error_response(  # noqa: PLR0913  # keyword-only envelope fields
    request: Request,
    status: int,
    message: str,
    *,
    code: str | None = None,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body = ErrorOut(
        error=ErrorBody(code=code or _CODES.get(status, "error"), message=message, details=details),
        request_id=request_id,
    )
    all_headers = dict(headers or {}) | SECURITY_HEADERS
    if request_id:
        all_headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        status_code=status,
        content=body.model_dump(exclude_none=True) | {"request_id": request_id},
        headers=all_headers or None,
    )


# Error responses every /api/v1 route may return; detail routes add 404.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorOut, "description": "Invalid cursor"},
    422: {"model": ErrorOut, "description": "Invalid parameter"},
    500: {"model": ErrorOut, "description": "Internal error"},
    503: {"model": ErrorOut, "description": "Database unavailable or query timed out"},
}
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorOut, "description": "Not found"}}


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # registered for this type only
        return await unhandled_exception_handler(request, exc)
    message = exc.detail if isinstance(exc.detail, str) else _CODES.get(exc.status_code, "Error")
    return _error_response(
        request, exc.status_code, message, headers=dict(exc.headers or {}) or None
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # registered for this type only
        return await unhandled_exception_handler(request, exc)
    details = [
        {
            "loc": [str(part) for part in err.get("loc", ())],
            "msg": str(err.get("msg", "")),
            "type": str(err.get("type", "")),
        }
        for err in exc.errors()
    ]
    return _error_response(request, 422, "Invalid request parameters", details=details)


async def database_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DBAPIError):  # registered for this type only
        return await unhandled_exception_handler(request, exc)
    request_id = getattr(request.state, "request_id", None)
    if isinstance(exc.orig, QueryCanceled):
        logger.warning("Query canceled (statement timeout)", extra={"request_id": request_id})
        return _error_response(request, 503, "Query timed out", code="query_timeout")
    if isinstance(exc, OperationalError):
        logger.warning("Database unavailable", extra={"request_id": request_id})
        return _error_response(request, 503, "Database unavailable", code="database_unavailable")
    return await unhandled_exception_handler(request, exc)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    logger.error("Unhandled exception", exc_info=exc, extra={"request_id": request_id})
    return _error_response(request, 500, "Internal server error")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(DBAPIError, database_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
