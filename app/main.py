"""FastAPI 应用装配（v1.1.0：前后端分离）。

- `/`         → 新 Vue3 SPA（frontend/dist；未构建时回退提示）
- `/legacy`   → 旧原生 HTML 前端（回退对照）
- `/api/*`    → REST + SSE 接口（含鉴权 /api/auth/*、蒸馏、Agent 平台 /api/playground/*）
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import api_router
from .api.mcp_http import register_mcp_routes
from .auth.db import init_db
from app.core.config import settings

# 初始化系统数据库（用户/OAuth 身份表），幂等
init_db()

ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = ROOT / "frontend" / "dist"  # 新 Vue SPA 构建产物

app = FastAPI(
    title="零号.奇点工坊",
    description=(
        "把历史业务数据逆向蒸馏成任意第三方 Agent 可零改动挂载的业务能力（MCP/Skill）。\n"
        "前端：Vue3 SPA（/）；旧版：/legacy；接口：/api/*。"
    ),
    version=__version__,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

# 远程 MCP 交付端点（/api/mcp/*）。须在挂载 SPA `/` 之前注册，避免被静态回退吞掉。
register_mcp_routes(app)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "llm_enabled": settings.llm_enabled,
        "llm_model": settings.llm_model if settings.llm_enabled else None,
        "frontend_built": DIST_DIR.exists(),
        "channels": {
            "spa": "/",
            "legacy": "/legacy",
            "auth_api": "/api/auth",
            "playground_api": "/api/playground",
        },
    }