"""Independent Agent platform API."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from app.domain.models import ChatMessage, ChatRequest
from app.playground import resources as pg_resources
from app.playground import service as pg
from ..auth.deps import get_current_user
from ..auth.models import PublicUser

router = APIRouter(prefix="/playground", tags=["playground"])

_ATTACHMENT_SUFFIX = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".txt", ".md", ".pdf", ".doc", ".docx"}
_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


@router.get("/resources")
def list_resources(user: PublicUser = Depends(get_current_user)) -> dict:
    return pg_resources.all_resources(user.id)


@router.get("/conversations")
def list_conversations(user: PublicUser = Depends(get_current_user)) -> dict:
    return {"conversations": pg.list_conversations(user.id)}


@router.post("/conversations")
def create_conversation(
    payload: dict | None = Body(default=None),
    user: PublicUser = Depends(get_current_user),
) -> dict:
    item = pg.create_conversation(user.id, str((payload or {}).get("title") or ""))
    return {"item": item, "conversations": pg.list_conversations(user.id)}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    user: PublicUser = Depends(get_current_user),
) -> dict:
    return {"conversations": pg.delete_conversation(user.id, conversation_id)}


@router.post("/skills")
async def install_skill(
    files: list[UploadFile] = File(...),
    paths: list[str] = Form(default=[]),
    name: str = Form(default=""),
    user: PublicUser = Depends(get_current_user),
) -> dict:
    try:
        item = await pg_resources.install_skill_from_files(user.id, files, paths=paths, name=name)
        pg.save_agent_config(user.id, pg.get_agent_config(user.id))
        return {"item": item, **pg_resources.all_resources(user.id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/skills/{skill_id}")
def delete_skill(skill_id: str, user: PublicUser = Depends(get_current_user)) -> dict:
    pg_resources.delete_skill(user.id, skill_id)
    pg.save_agent_config(user.id, pg.get_agent_config(user.id))
    return pg_resources.all_resources(user.id)


@router.post("/sandboxes")
def save_sandbox(
    payload: dict | None = Body(default=None),
    user: PublicUser = Depends(get_current_user),
) -> dict:
    try:
        item = pg_resources.public_sandbox_resource(pg_resources.save_sandbox(user.id, payload or {}))
        return {"item": item, **pg_resources.all_resources(user.id)}
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/sandboxes/{sandbox_id}")
def delete_sandbox(sandbox_id: str, user: PublicUser = Depends(get_current_user)) -> dict:
    try:
        pg_resources.delete_sandbox(user.id, sandbox_id)
        pg.save_agent_config(user.id, pg.get_agent_config(user.id))
        return pg_resources.all_resources(user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sandboxes/{sandbox_id}/install")
def install_sandbox_dependencies(
    sandbox_id: str,
    payload: dict | None = Body(default=None),
    user: PublicUser = Depends(get_current_user),
) -> dict:
    try:
        cfg = (payload or {}).get("agent_config") or pg.get_agent_config(user.id)
        item = pg_resources.public_sandbox_resource(
            pg_resources.install_sandbox_dependencies(user.id, sandbox_id, cfg)
        )
        return {"item": item, **pg_resources.all_resources(user.id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/mcps")
async def save_mcp(
    payload: dict | None = Body(default=None),
    user: PublicUser = Depends(get_current_user),
) -> dict:
    try:
        item = await pg_resources.save_mcp(user.id, payload or {})
        pg.save_agent_config(user.id, pg.get_agent_config(user.id))
        return {"item": item, **pg_resources.all_resources(user.id)}
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/mcps/{mcp_id}")
def delete_mcp(mcp_id: str, user: PublicUser = Depends(get_current_user)) -> dict:
    pg_resources.delete_mcp(user.id, mcp_id)
    pg.save_agent_config(user.id, pg.get_agent_config(user.id))
    return pg_resources.all_resources(user.id)


@router.post("/llms")
def save_llm(
    payload: dict | None = Body(default=None),
    user: PublicUser = Depends(get_current_user),
) -> dict:
    try:
        item = pg_resources.save_llm(user.id, payload or {})
        return {"item": item, **pg_resources.all_resources(user.id)}
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/llms/{llm_id}")
def delete_llm(llm_id: str, user: PublicUser = Depends(get_current_user)) -> dict:
    pg_resources.delete_llm(user.id, llm_id)
    pg.save_agent_config(user.id, pg.get_agent_config(user.id))
    return pg_resources.all_resources(user.id)


@router.post("/chat")
async def playground_chat(
    req: ChatRequest, user: PublicUser = Depends(get_current_user)
) -> StreamingResponse:
    return StreamingResponse(
        pg.stream_chat(user.id, req.message.strip(), req.conversation_id),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/messages", response_model=list[ChatMessage])
def get_messages(
    conversation_id: str = Query(default=""),
    user: PublicUser = Depends(get_current_user),
) -> list[ChatMessage]:
    return pg.get_messages(user.id, conversation_id)


@router.delete("/messages")
def clear_messages(
    conversation_id: str = Query(default=""),
    user: PublicUser = Depends(get_current_user),
) -> dict:
    pg.clear_messages(user.id, conversation_id)
    return {"message": "已清空 Agent 平台对话历史"}


@router.get("/agent-config")
def get_agent_config(user: PublicUser = Depends(get_current_user)) -> dict:
    return pg.get_agent_config(user.id)


@router.put("/agent-config")
def save_agent_config(
    payload: dict | None = Body(default=None),
    user: PublicUser = Depends(get_current_user),
) -> dict:
    return pg.save_agent_config(user.id, payload or {})


@router.get("/attachments")
def list_attachments(
    conversation_id: str = Query(default=""),
    user: PublicUser = Depends(get_current_user),
) -> dict:
    return {"files": pg.list_attachments(user.id, conversation_id)}


@router.post("/attachments")
async def upload_attachments(
    files: list[UploadFile] = File(...),
    conversation_id: str = Query(default=""),
    user: PublicUser = Depends(get_current_user),
) -> dict:
    dest_dir = pg.attachments_dir(user.id, conversation_id)
    saved = []
    for upload in files:
        filename = Path(upload.filename or "").name
        suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if not filename or suffix not in _ATTACHMENT_SUFFIX:
            raise HTTPException(status_code=400, detail=f"不支持的附件类型：{filename}")
        dest = dest_dir / filename
        dest.write_bytes(await upload.read())
        saved.append(filename)
    return {"message": f"已上传 {len(saved)} 个附件", "files": saved}


@router.delete("/attachments")
def clear_attachments(
    conversation_id: str = Query(default=""),
    user: PublicUser = Depends(get_current_user),
) -> dict:
    removed = pg.clear_attachments(user.id, conversation_id)
    return {"message": f"已清空 {removed} 个附件"}
