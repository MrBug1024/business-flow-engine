"""FastAPI 应用装配（v1.0.5：蒸馏通道 + 验证通道双前端）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import api_router
from .config import settings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(
    title="业务流逆向工程引擎",
    description=(
        "基于历史业务数据逆向复刻业务流程，蒸馏为可复用 Skill 包。\n"
        "蒸馏通道（/）：推导 ER + 流程 + 生成 Skill。\n"
        "验证通道（/verify）：挂载 Skill 包，执行验证（与平台代码隔离）。"
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


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "llm_enabled": settings.llm_enabled,
        "llm_model": settings.llm_model if settings.llm_enabled else None,
        "channels": {
            "distillation": "/",
            "verification": "/verify",
        },
    }


@app.get("/")
def index() -> FileResponse:
    """蒸馏通道前端（推导 + 生成技能）。"""
    return FileResponse(WEB_DIR / "index.html")


@app.get("/verify")
def verify_index() -> FileResponse:
    """验证通道前端（Skill 包执行验证，与平台隔离）。"""
    vfile = WEB_DIR / "verify.html"
    if vfile.exists():
        return FileResponse(vfile)
    return FileResponse(WEB_DIR / "index.html")


if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
