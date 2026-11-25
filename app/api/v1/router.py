from fastapi import APIRouter

from app.api.v1 import analysis, catalog
from app.core.errors import ERROR_RESPONSES

router = APIRouter(responses=ERROR_RESPONSES)
router.include_router(catalog.router)
router.include_router(analysis.router)
