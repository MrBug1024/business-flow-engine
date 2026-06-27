"""FastAPI 应用装配。"""

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
    description="基于业务历史数据逆向复刻业务流程，并固化为可复用技能库。",
    version=__version__,
)

# 开发期放开跨域，便于前端独立调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/api/health")
def health() -> dict:
    """健康检查，并暴露当前是否启用 LLM。"""
    return {
        "status": "ok",
        "version": __version__,
        "llm_enabled": settings.llm_enabled,
        "llm_model": settings.llm_model if settings.llm_enabled else None,
    }


@app.get("/")
def index() -> FileResponse:
    """返回前端单页应用。"""
    return FileResponse(WEB_DIR / "index.html")


# 静态资源（若后续拆分 css/js 可放入 web/ 下）
if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
