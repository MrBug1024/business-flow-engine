"""验证通道接口（v1.0.5）。

与蒸馏通道 /api/scenarios/{id}/chat 完全分离：
- 使用独立的验证 Agent（只有 Skill 包工具）
- 独立的对话历史（verify_chat.jsonl）
- 独立的 LLM 会话
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from ..models import ChatMessage, ChatRequest
from ..storage import store
from ..verify_service import stream_verify
from .deps import get_owned_scenario_or_404, get_scenario_or_404

# 所有端点均为 /scenarios/{scenario_id}/verify/...，路由级强制登录 + 归属校验。
router = APIRouter(tags=["verify"], dependencies=[Depends(get_owned_scenario_or_404)])

_ALLOWED_SUFFIX = {".csv", ".tsv", ".xlsx", ".xls", ".json"}

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/scenarios/{scenario_id}/verify/chat")
async def verify_chat(scenario_id: str, req: ChatRequest) -> StreamingResponse:
    """验证通道流式对话（SSE）。

    与蒸馏通道隔离：只能调用 Skill 包中的工具，不能调用平台内部工具。
    技能包必须已在蒸馏通道生成，否则返回引导信息。
    """
    scenario = get_scenario_or_404(scenario_id)
    return StreamingResponse(
        stream_verify(scenario, req.message.strip()),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/scenarios/{scenario_id}/verify/messages", response_model=list[ChatMessage])
def get_verify_messages(scenario_id: str) -> list[ChatMessage]:
    """获取验证通道的历史对话记录（与蒸馏通道隔离）。"""
    get_scenario_or_404(scenario_id)
    return store.get_verify_messages(scenario_id)


@router.post("/scenarios/{scenario_id}/verify/uploads")
async def upload_verify_data(scenario_id: str, files: list[UploadFile]) -> dict:
    """上传验证通道专用的「新业务数据」——与蒸馏通道的 uploads/ 物理隔离。

    文件名（不含后缀）须与技能包 domain_knowledge.json 里的表名一致，
    验证 Agent 才能识别应该把哪个文件当哪张表用。上传后，验证通道的
    execute_skill / query_data 等工具会优先使用这批新数据，而不是蒸馏时的旧数据。
    """
    get_scenario_or_404(scenario_id)
    dest_dir = Path(store.verify_uploads_dir(scenario_id))
    saved = []
    for upload in files:
        suffix = "." + upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
        if suffix not in _ALLOWED_SUFFIX:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型：{upload.filename}（支持 CSV/TSV/Excel/JSON）",
            )
        dest = dest_dir / upload.filename
        dest.write_bytes(await upload.read())
        saved.append(upload.filename)
    return {"message": f"已上传 {len(saved)} 个验证测试文件", "files": saved}


@router.get("/scenarios/{scenario_id}/verify/uploads")
def list_verify_uploads(scenario_id: str) -> dict:
    """列出当前验证通道暂存的「新业务数据」文件（为空则验证通道会退回使用蒸馏数据）。"""
    get_scenario_or_404(scenario_id)
    dest_dir = Path(store.verify_uploads_dir(scenario_id))
    files = sorted(
        f.name for f in dest_dir.iterdir()
        if f.is_file() and f.suffix.lower() in _ALLOWED_SUFFIX
    ) if dest_dir.exists() else []
    return {"files": files, "using_verify_data": bool(files)}


@router.delete("/scenarios/{scenario_id}/verify/uploads")
def clear_verify_uploads(scenario_id: str) -> dict:
    """清空验证通道的暂存测试数据，之后验证通道会退回使用蒸馏阶段的原始数据。"""
    get_scenario_or_404(scenario_id)
    dest_dir = Path(store.verify_uploads_dir(scenario_id))
    removed = 0
    if dest_dir.exists():
        for f in dest_dir.iterdir():
            if f.is_file():
                f.unlink()
                removed += 1
    return {"message": f"已清空 {removed} 个验证测试文件，验证通道将退回使用蒸馏数据"}


@router.get("/scenarios/{scenario_id}/verify/status")
def get_verify_status(scenario_id: str) -> dict:
    """查询当前场景的验证就绪状态。"""
    scenario = get_scenario_or_404(scenario_id)
    skills_dir = Path(store.skills_dir(scenario_id))
    manifest_exists = (skills_dir / "manifest.json").exists()
    executor_exists = (skills_dir / "main_skill" / "scripts" / "skill_executor.py").exists()
    verify_dir = Path(store.verify_uploads_dir(scenario_id))
    verify_files = sorted(
        f.name for f in verify_dir.iterdir()
        if f.is_file() and f.suffix.lower() in _ALLOWED_SUFFIX
    ) if verify_dir.exists() else []
    return {
        "scenario_id": scenario_id,
        "scenario_name": scenario.name,
        "skills_generated": bool(scenario.skills),
        "manifest_ready": manifest_exists,
        "executor_ready": executor_exists,
        "verify_ready": bool(scenario.skills) and executor_exists,
        "status": scenario.status.value,
        "verify_data_files": verify_files,
        "using_verify_data": bool(verify_files),
    }
