"""HTTP 接口层（v1.0.5：蒸馏通道 + 验证通道分离）。

接口划分：
* /scenarios/{id}/chat        — 蒸馏通道（推关联/推流程/生成技能）
* /scenarios/{id}/verify/chat — 验证通道（Skill 包执行，与平台隔离）
* 其余 REST 接口：场景管理、文件上传、图谱查询等
"""

from fastapi import APIRouter

from . import chat, files, graph, scenarios, verify

# 汇总所有子路由
api_router = APIRouter(prefix="/api")
api_router.include_router(scenarios.router)
api_router.include_router(files.router)
api_router.include_router(chat.router)
api_router.include_router(graph.router)
api_router.include_router(verify.router)
