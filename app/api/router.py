from fastapi import APIRouter

from app.api import health
from app.api.v1.router import router as v1_router

api_router = APIRouter()
api_router.include_router(health.router)  # unversioned: used by probes and orchestrators
api_router.include_router(v1_router, prefix="/api/v1")
