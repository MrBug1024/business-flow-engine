"""FastAPI 应用装配（v1.1.0：前后端分离）。

- `/`         → 新 Vue3 SPA（frontend/dist；未构建时回退提示）
- `/legacy`   → 旧原生 HTML 前端（回退对照）
- `/api/*`    → REST + SSE 接口（含鉴权 /api/auth/*、蒸馏、沙盒 /api/playground/*）
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
from .config import settings

# 初始化系统数据库（用户/OAuth 身份表），幂等
init_db()

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"                 # 旧原生 UI（挂 /legacy）
DIST_DIR = ROOT / "frontend" / "dist"  # 新 Vue SPA 构建产物

app = FastAPI(
    title="业务流逆向工程引擎",
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


# --------------------------------------------------------------- 旧原生 UI（/legacy）
@app.get("/legacy")
def legacy_index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/legacy/verify")
def legacy_verify() -> FileResponse:
    vfile = WEB_DIR / "verify.html"
    return FileResponse(vfile if vfile.exists() else WEB_DIR / "index.html")


if WEB_DIR.exists():
    # 旧 index.html 通过绝对路径引用 /web/app.js
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


# --------------------------------------------------------------- 新 Vue SPA（/）
_NOT_BUILT_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>前端未构建</title><style>body{font-family:system-ui;background:#0d1117;color:#e6edf3;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{max-width:560px;line-height:1.8}code{background:#1c2230;padding:2px 8px;border-radius:6px;color:#58c8e3}
a{color:#8b6ef5}</style></head><body><div class="box">
<h2>前端尚未构建</h2>
<p>开发模式：进入 <code>frontend/</code> 执行 <code>npm install</code> 后 <code>npm run dev</code>，
浏览 <a href="http://127.0.0.1:5173">http://127.0.0.1:5173</a>（已代理 /api 到本服务）。</p>
<p>生产模式：<code>npm run build</code> 生成 <code>frontend/dist</code> 后刷新本页。</p>
<p>或使用旧版界面：<a href="/legacy">/legacy</a></p>
</div></body></html>"""

if DIST_DIR.exists():
    # html=True：/ 返回 index.html，静态资源直接命中；hash 路由无需 catch-all 回退
    app.mount("/", StaticFiles(directory=DIST_DIR, html=True), name="spa")
else:
    @app.get("/")
    def spa_placeholder() -> HTMLResponse:
        return HTMLResponse(_NOT_BUILT_HTML)
