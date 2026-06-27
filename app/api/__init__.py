"""HTTP 接口层。

接口划分（遵循需求约定）：
* 大部分「与 AI 协作完成任务」走流式接口 `/chat`（SSE）。
* 对话记录、关系图谱、流程图谱、新增/删除业务场景等走专用 REST 接口。
"""

from fastapi import APIRouter

from . import chat, files, graph, scenarios

# 汇总所有子路由
api_router = APIRouter(prefix="/api")
api_router.include_router(scenarios.router)
api_router.include_router(files.router)
api_router.include_router(chat.router)
api_router.include_router(graph.router)
