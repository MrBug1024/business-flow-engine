"""第三方发布包接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.playground import service as pg
from app.release import builder as release_builder
from app.domain.models import Scenario, ScenarioStatus
from .deps import get_owned_scenario_or_404

router = APIRouter(tags=["release"], dependencies=[Depends(get_owned_scenario_or_404)])


class DockerPublishRequest(BaseModel):
    registry: str = Field(default="harbor.gshbzw.com/skills")
    repository: str = ""
    tag: str = Field(default="1.0.0")


@router.post("/scenarios/{scenario_id}/release/build")
def build_release(scenario_id: str, request: Request) -> dict:
    """构建标准第三方发布包。"""
    try:
        return release_builder.build_release_package(
            scenario_id, base_url=pg.public_base_url(request)
        ).as_dict(base_url=pg.public_base_url(request))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/scenarios/{scenario_id}/release/status")
def get_release_status(scenario_id: str, request: Request) -> dict:
    try:
        return release_builder.release_status(scenario_id, base_url=pg.public_base_url(request))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/scenarios/{scenario_id}/release/config")
def get_release_config(scenario_id: str, request: Request) -> dict:
    """返回蒸馏完成后的发布配置：标准 Skill、MCP、Docker、安装提示。"""
    cfg = pg.mount_config(scenario_id, pg.public_base_url(request))
    if not cfg:
        raise HTTPException(status_code=404, detail="发布配置不存在或能力包尚未生成")
    return cfg


@router.get("/scenarios/{scenario_id}/release/download/{artifact}")
def download_release_artifact(scenario_id: str, artifact: str) -> FileResponse:
    try:
        path = release_builder.artifact_path(scenario_id, artifact)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"发布物不存在：{artifact}")
    name = artifact.strip().lower()
    if name.startswith("skill"):
        filename = "skill.zip"
    elif name.startswith("mcp"):
        filename = "mcp.zip"
    else:
        filename = "toolplane-docker.zip"
    return FileResponse(path, filename=filename, media_type="application/zip")


@router.post("/scenarios/{scenario_id}/release/docker/publish")
def publish_docker_image(
    scenario_id: str,
    payload: DockerPublishRequest,
    request: Request,
    scenario: Scenario = Depends(get_owned_scenario_or_404),
) -> dict:
    """高级兼容：验证通过后构建、标记并推送 Docker MCP 镜像。"""
    if scenario.status != ScenarioStatus.ACTIVE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"当前场景状态为 {scenario.status.value}，尚未记录为验证通过(active)，"
                "不能发布兼容 Docker 镜像。请先在 Agent 平台完成验证，并让 AI 明确输出“验证通过”。"
            ),
        )
    try:
        return release_builder.publish_docker_image(
            scenario_id,
            registry=payload.registry,
            repository=payload.repository,
            tag=payload.tag,
            base_url=pg.public_base_url(request),
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
