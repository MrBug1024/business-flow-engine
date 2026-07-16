"""FastAPI application for AI Business Studio."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import api_router
from app.core.config import settings
from app.studio.settings import studio_settings


ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = ROOT / "frontend" / "dist"

app = FastAPI(
    title="AI Business Studio",
    description=(
        "AI-native workspace with configurable models and dynamically discovered "
        "Tool, Skill, and MCP capabilities."
    ),
    version=__version__,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/api/health")
def health() -> dict:
    active_model = studio_settings.active_model_config()
    active_key = active_model.api_key.strip() or settings.openai_api_key.strip()
    active_base_url = active_model.base_url.strip() or settings.openai_base_url.strip()
    return {
        "status": "ok",
        "version": __version__,
        "llm_enabled": bool(active_key and active_base_url),
        "llm_model": active_model.model,
        "frontend_built": DIST_DIR.exists(),
        "channels": {
            "spa": "/",
            "studio_api": "/api/businesses",
            "settings_api": "/api/settings",
        },
    }


if DIST_DIR.exists():
    assets_dir = DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@app.get("/", response_model=None)
def spa_index():
    index_file = DIST_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse(
        "<h1>AI Business Studio</h1><p>Frontend is not built yet. Run npm run build in frontend.</p>",
        status_code=503,
    )


@app.get("/{full_path:path}", response_model=None)
def spa_fallback(full_path: str):
    if full_path.startswith("api/"):
        return HTMLResponse('{"detail":"Not Found"}', status_code=404, media_type="application/json")
    target = DIST_DIR / full_path
    if target.exists() and target.is_file():
        return FileResponse(target)
    return spa_index()
