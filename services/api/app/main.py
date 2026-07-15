from __future__ import annotations

import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.repository import create_project, list_projects
from app.schemas import ProjectBrief

settings = get_settings()

# The slim runtime image ships no /etc/mime.types, so Python's mimetypes database
# misses .webp/.vtt and StaticFiles falls back to application/octet-stream.
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("text/vtt", ".vtt")
TASK_MODE_ALLOWED_PATHS = {
    "/api/health",
    "/api/readiness",
    "/api/function-compute/tasks",
}


def task_mode_allows_path(path: str) -> bool:
    return path in TASK_MODE_ALLOWED_PATHS


def discover_frontend_dist_path(api_file: Path) -> Path:
    for parent in api_file.parents:
        candidate = parent / "apps" / "web" / "dist"
        if candidate.exists():
            return candidate
    return api_file.parent / "_missing_frontend_dist"


def frontend_dist_path() -> Path:
    if settings.frontend_dist is not None:
        return settings.frontend_dist
    return discover_frontend_dist_path(Path(__file__).resolve())


def initialize_startup_state() -> None:
    if settings.state_backend == "oss":
        return
    init_db()
    if settings.seed_demo:
        with SessionLocal() as session:
            if not list_projects(session):
                create_project(
                    session,
                    ProjectBrief(
                        title="The Last Delivery",
                        premise="Every night a small courier robot leaves a package at the same apartment. On its final night before decommissioning, the door finally opens.",
                        duration_seconds=28,
                        budget_usd=18,
                    ),
                    settings=settings,
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_startup_state()
    yield


app = FastAPI(
    title="DirectorGraph API",
    version="0.1.0",
    description="A self-correcting, budget-aware AI showrunner built on Qwen Cloud.",
    lifespan=lifespan,
    # Behind the edge proxy only /api/* reaches uvicorn, so the default /docs and
    # /openapi.json would be swallowed by the SPA. Serve them under /api instead.
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.middleware("http")
async def task_mode_route_guard(request: Request, call_next):
    if settings.app_mode == "task" and not task_mode_allows_path(request.url.path):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return await call_next(request)


app.mount("/media", StaticFiles(directory=settings.media_root), name="media")
frontend_dist = frontend_dist_path()
frontend_index = frontend_dist / "index.html"
if frontend_index.exists():
    assets = frontend_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")


@app.get("/", response_model=None)
def root():
    if frontend_index.exists():
        return FileResponse(frontend_index)
    return {"name": settings.app_name, "docs": "/docs", "health": "/api/health"}


if frontend_index.exists():

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    def spa_fallback(full_path: str):
        if full_path == "api" or full_path.startswith(("api/", "media/")):
            raise HTTPException(status_code=404, detail="Not found")
        requested = (frontend_dist / full_path).resolve()
        if frontend_dist in requested.parents and requested.is_file():
            return FileResponse(requested)
        return FileResponse(frontend_index)
