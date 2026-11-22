import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.middleware import REQUEST_ID_HEADER

logger = logging.getLogger(__name__)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    logger.error("Unhandled exception", exc_info=exc, extra={"request_id": request_id})
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id},
        headers={REQUEST_ID_HEADER: request_id} if request_id else None,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(Exception, unhandled_exception_handler)
