"""对话接口。

* `POST /chat`：核心流式接口（SSE）。绝大多数「与 AI 协作完成任务」都经此完成。
* `GET /messages`：拉取历史对话记录（专用接口）。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..chat_service import stream_chat
from ..models import ChatMessage, ChatRequest
from ..storage import store
from .deps import get_scenario_or_404

router = APIRouter(tags=["chat"])

# SSE 必要响应头：禁用缓存与反向代理缓冲，确保逐帧实时下发
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/scenarios/{scenario_id}/chat")
async def chat(scenario_id: str, req: ChatRequest) -> StreamingResponse:
    """与 AI 流式对话。返回 SSE 事件流（thinking/content/tool_call/refresh/...）。"""
    scenario = get_scenario_or_404(scenario_id)
    return StreamingResponse(
        stream_chat(scenario, req.message.strip()),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/scenarios/{scenario_id}/messages", response_model=list[ChatMessage])
def get_messages(scenario_id: str) -> list[ChatMessage]:
    """获取业务场景的历史对话记录。"""
    get_scenario_or_404(scenario_id)
    return store.get_messages(scenario_id)
