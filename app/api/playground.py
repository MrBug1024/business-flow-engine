"""通用第三方沙盒接口（Playground）——按用户隔离。

把「验证通道」升级为「默认第三方 Agent 平台 + 配置面板」：
- catalog：能力市场（当前用户拥有的、已生成能力包的场景）
- mounts：安装/卸载能力（等价于在 MCP 配置里增删一条 server）
- config：某能力的能力卡片 + 粘贴即用 MCP 配置片段（配置面板）
- uploads：给某已挂载场景上传测试数据（复用场景的 verify_uploads 目录）
- chat：通用沙盒流式对话（Agent 不预置业务知识，自主发现 + 决策）
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.playground import service as pg
from ..auth.deps import get_current_user
from ..auth.models import PublicUser
from app.domain.models import ChatMessage, ChatRequest
from app.domain.storage import store

router = APIRouter(prefix="/playground", tags=["playground"])

_ALLOWED_SUFFIX = {".csv", ".tsv", ".xlsx", ".xls", ".json"}
_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


def _require_owned(scenario_id: str, user: PublicUser) -> None:
    sc = store.get(scenario_id)
    if sc is None or sc.owner_id != user.id:
        raise HTTPException(status_code=404, detail="场景不存在")


@router.get("/catalog")
def get_catalog(user: PublicUser = Depends(get_current_user)) -> dict:
    """能力市场：列出当前用户拥有的、已生成 MCP 能力包的场景（含是否已挂载）。"""
    return {"items": pg.catalog(user.id), "mounted": pg.get_mounts(user.id)}


@router.get("/mounts")
def list_mounts(user: PublicUser = Depends(get_current_user)) -> dict:
    return {"mounted": pg.get_mounts(user.id)}


@router.post("/mounts/{scenario_id}")
def mount_scenario(scenario_id: str, user: PublicUser = Depends(get_current_user)) -> dict:
    _require_owned(scenario_id, user)
    pkg_dir = Path(store.skills_dir(scenario_id))
    if not (pkg_dir / "main_skill").exists() or not (pkg_dir / "mcp.json").exists():
        raise HTTPException(status_code=400, detail="该场景尚未生成 MCP 能力包，无法挂载")
    return {"mounted": pg.mount(user.id, scenario_id)}


@router.delete("/mounts/{scenario_id}")
def unmount_scenario(scenario_id: str, user: PublicUser = Depends(get_current_user)) -> dict:
    return {"mounted": pg.unmount(user.id, scenario_id)}


@router.get("/mounts/{scenario_id}/config")
def get_mount_config(
    scenario_id: str, request: Request, user: PublicUser = Depends(get_current_user)
) -> dict:
    _require_owned(scenario_id, user)
    cfg = pg.mount_config(scenario_id, pg.public_base_url(request))
    if not cfg:
        raise HTTPException(status_code=404, detail="能力包不存在或未生成 MCP 描述符")
    return cfg


@router.post("/chat")
async def playground_chat(
    req: ChatRequest, user: PublicUser = Depends(get_current_user)
) -> StreamingResponse:
    """通用沙盒流式对话（SSE）。基于当前用户挂载集合构建通用 Agent。"""
    return StreamingResponse(
        pg.stream_chat(user.id, req.message.strip()),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/messages", response_model=list[ChatMessage])
def get_messages(user: PublicUser = Depends(get_current_user)) -> list[ChatMessage]:
    return pg.get_messages(user.id)


@router.delete("/messages")
def clear_messages(user: PublicUser = Depends(get_current_user)) -> dict:
    pg.clear_messages(user.id)
    return {"message": "已清空沙盒对话历史"}


@router.post("/scenarios/{scenario_id}/uploads")
async def upload_test_data(
    scenario_id: str, files: list[UploadFile], user: PublicUser = Depends(get_current_user)
) -> dict:
    """给某已挂载场景上传测试数据（存入该场景的 verify_uploads/，沙盒执行时优先使用）。"""
    _require_owned(scenario_id, user)
    dest_dir = Path(store.verify_uploads_dir(scenario_id))
    saved = []
    for upload in files:
        suffix = "." + upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
        if suffix not in _ALLOWED_SUFFIX:
            raise HTTPException(status_code=400, detail=f"不支持的文件类型：{upload.filename}")
        (dest_dir / upload.filename).write_bytes(await upload.read())
        saved.append(upload.filename)
    return {"message": f"已为场景上传 {len(saved)} 个测试文件", "files": saved}


@router.get("/scenarios/{scenario_id}/uploads")
def list_test_data(scenario_id: str, user: PublicUser = Depends(get_current_user)) -> dict:
    _require_owned(scenario_id, user)
    dest_dir = Path(store.verify_uploads_dir(scenario_id))
    files = sorted(
        f.name for f in dest_dir.iterdir()
        if f.is_file() and f.suffix.lower() in _ALLOWED_SUFFIX
    ) if dest_dir.exists() else []
    return {"files": files, "using_test_data": bool(files)}
