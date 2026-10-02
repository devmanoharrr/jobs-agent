from fastapi import FastAPI

from app.api.dashboard import router as dashboard_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.sources import router as sources_router

app = FastAPI(title="India Job Ingestion Agent", version="0.1.0")
app.include_router(dashboard_router)
app.include_router(health_router)
app.include_router(jobs_router)
app.include_router(sources_router)
