"""对外交付：把已生成的场景能力包作为**远程 MCP Server** 通过 HTTP(SSE) 暴露。

第三方宿主（Claude Desktop / Cursor / Cline 等）无需接触本机文件，只要一个可访问的
URL 即可挂载本平台能力：

    GET  /api/mcp/{scenario_id}/sse        —— 建立 SSE 会话（server→client 事件流）
    POST /api/mcp/messages/?session_id=...  —— client→server 消息（由 SSE 事件回告地址）

安装链接的基址：开发/测试按请求主机自动推导（本机 IP / 域名）；正式环境在 `.env`
配置 `MCP_PUBLIC_BASE_URL`。可选 `MCP_ACCESS_TOKEN` 为端点加一道 Bearer 令牌。

会话与业务逻辑复用平台无关的 `scenario_runtime`，执行不依赖平台 store/数据库。
"""

from __future__ import annotations

from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from mcp.server.sse import SseServerTransport
from mcp.server.transport_security import TransportSecuritySettings

from app.core.config import settings
from app.domain.storage import store
from app.runtime import mcp_server
from app.runtime import scenario_runtime as rt

# 面向公网第三方：关闭 DNS-rebinding 校验（否则任意 IP/域名的 Host 会被拒）。
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)
# 单一全局 transport：会话以 session_id 关联，POST 消息按会话路由到对应 server，
# 与是哪个场景无关，因此一个 messages 端点即可服务所有场景。
_MESSAGES_PATH = "/api/mcp/messages/"
_sse = SseServerTransport(_MESSAGES_PATH, security_settings=_SECURITY)


# --------------------------------------------------------------------- 鉴权
def _extract_token(scope) -> str | None:
    headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
    auth = headers.get(b"authorization")
    if auth:
        val = auth.decode("latin-1")
        if val.lower().startswith("bearer "):
            return val[7:].strip()
    qs = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    tok = qs.get("token")
    return tok[0] if tok else None


def _token_ok(scope) -> bool:
    required = settings.mcp_access_token.strip()
    if not required:
        return True
    return _extract_token(scope) == required


# ---------------------------------------------------------------- 能力包装载
def package_for(scenario_id: str):
    """按场景 ID 取已生成的能力包；不存在/未就绪返回 None（不产生任何目录副作用）。"""
    pkg_dir = store.scenario_dir(scenario_id) / "skills"
    if not (pkg_dir / "main_skill").exists() or not (pkg_dir / "mcp.json").exists():
        return None
    pkg = rt.ScenarioPackage.load(pkg_dir)
    return pkg if pkg.is_ready() else None


# ------------------------------------------------------------------ SSE (GET)
async def handle_sse(request: Request):
    scenario_id = request.path_params["scenario_id"]
    if not _token_ok(request.scope):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    pkg = package_for(scenario_id)
    if pkg is None:
        return JSONResponse(
            {"error": f"能力包不存在或未生成：{scenario_id}"}, status_code=404
        )

    server = mcp_server.build_server([pkg])
    async with _sse.connect_sse(request.scope, request.receive, request._send) as (read, write):
        await server.run(read, write, server.create_initialization_options())
    return Response(status_code=204)


# -------------------------------------------------------------- messages (POST)
async def handle_messages(scope, receive, send) -> None:
    """messages 端点的 ASGI 包装：先做可选令牌校验，再交给 SSE transport。"""
    if scope["type"] == "http" and not _token_ok(scope):
        resp = JSONResponse({"error": "unauthorized"}, status_code=401)
        await resp(scope, receive, send)
        return
    await _sse.handle_post_message(scope, receive, send)


def register_mcp_routes(app) -> None:
    """把远程 MCP 路由注册到 FastAPI app（须在挂载 SPA `/` 之前调用）。"""
    app.router.routes.append(Route("/api/mcp/{scenario_id}/sse", endpoint=handle_sse))
    app.router.routes.append(Mount(_MESSAGES_PATH, app=handle_messages))
