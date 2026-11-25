import logging

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import DbSession
from app.core.errors import ErrorOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness: the process is up and serving requests."""
    return {"status": "alive"}


@router.get("/ready", responses={503: {"model": ErrorOut, "description": "Database down"}})
async def ready(db: DbSession) -> dict[str, str]:
    """Readiness: the service can reach its database."""
    try:
        await db.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("Readiness check failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        ) from exc
    return {"status": "ready"}
