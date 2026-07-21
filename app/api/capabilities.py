"""Capability registry APIs for AI Business Studio."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile

from app.auth.dependencies import current_account
from app.studio.capabilities.mcp import (
    merge_masked_mcp_configs,
    normalize_mcp_payload,
    probe_mcp_configs,
)
from app.studio.models import (
    InstallSkillFromUrlRequest,
    MCPServersRequest,
    StudioSettings,
    UpdateMCPServerRequest,
    UpdateStudioSettings,
)
from app.studio.capabilities.registry import list_skills
from app.studio.settings import studio_settings
from app.studio.capabilities.skill_installer import (
    MAX_SKILL_FILES,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_TOTAL_BYTES,
    SkillDownloadError,
    delete_user_skill,
    install_skill_files,
    install_skill_from_url,
)
from app.studio.capabilities.tools import tool_registry

router = APIRouter(tags=["capabilities"])


@router.get("/tools")
def list_tools() -> list[dict]:
    return [tool.model_dump(mode="json") for tool in tool_registry.list()]


@router.post("/tools/rescan")
def rescan_tools() -> dict:
    tools = tool_registry.refresh()
    return {
        "generation": tool_registry.generation,
        "tools": [tool.model_dump(mode="json") for tool in tools],
        "mounted": sum(1 for tool in tools if tool.mounted),
        "errors": sum(1 for tool in tools if tool.status != "ready"),
    }


@router.get("/skills")
def skills() -> list[dict]:
    return [skill.model_dump() for skill in list_skills(current_account().id)]


@router.get("/user-skills")
def user_skills() -> list[dict]:
    return [
        skill.model_dump()
        for skill in list_skills(current_account().id)
        if skill.kind == "user"
    ]


@router.post("/skills/install/upload", status_code=201)
async def install_uploaded_skill(
    files: list[UploadFile] = File(...),
    paths: list[str] | None = Form(default=None),
    install_consent: str | None = Header(default=None, alias="X-Studio-Install-Consent"),
) -> dict:
    _require_skill_install_consent(install_consent)
    if not files or len(files) > MAX_SKILL_FILES:
        raise HTTPException(status_code=422, detail=f"Skill upload accepts 1 to {MAX_SKILL_FILES} files.")
    if paths is not None and len(paths) != len(files):
        raise HTTPException(status_code=422, detail="paths must contain one relative path for every uploaded file.")
    entries: list[tuple[str, bytes]] = []
    total = 0
    try:
        for index, upload in enumerate(files):
            content = await upload.read(MAX_SKILL_FILE_BYTES + 1)
            if len(content) > MAX_SKILL_FILE_BYTES:
                raise HTTPException(status_code=413, detail=f"Skill file '{upload.filename}' is too large.")
            total += len(content)
            if total > MAX_SKILL_TOTAL_BYTES:
                raise HTTPException(status_code=413, detail="Skill folder exceeds the total upload size limit.")
            entries.append((paths[index] if paths is not None else upload.filename or "", content))
    finally:
        for upload in files:
            await upload.close()
    try:
        skill = install_skill_files(entries, current_account().id)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _installed_skill_response(skill, current_account().id)


@router.post("/skills/install/url", status_code=201)
def install_url_skill(
    req: InstallSkillFromUrlRequest,
    install_consent: str | None = Header(default=None, alias="X-Studio-Install-Consent"),
) -> dict:
    _require_skill_install_consent(install_consent)
    try:
        skill = install_skill_from_url(req.url, current_account().id)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SkillDownloadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _installed_skill_response(skill, current_account().id)


@router.delete("/skills/{name}")
def delete_skill(name: str) -> dict:
    try:
        skill = delete_user_skill(name, current_account().id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Skill not found.") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    owner_id = current_account().id
    current = studio_settings.load(owner_id)
    current.installed_skills = [item for item in current.installed_skills if item != name]
    saved = studio_settings.save(current, owner_id)
    return {
        "deleted": skill.model_dump(),
        "settings": studio_settings.public(saved, owner_id).model_dump(mode="json"),
    }


@router.get("/settings", response_model=StudioSettings)
def get_settings() -> StudioSettings:
    return studio_settings.public(owner_id=current_account().id)


@router.patch("/settings", response_model=StudioSettings)
def patch_settings(req: UpdateStudioSettings) -> StudioSettings:
    try:
        owner_id = current_account().id
        return studio_settings.public(
            studio_settings.update(req, owner_id),
            owner_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/models/{model_id}", response_model=StudioSettings)
def delete_model(model_id: str) -> StudioSettings:
    try:
        owner_id = current_account().id
        updated = studio_settings.delete_model(model_id, owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Model not found.")
    return studio_settings.public(updated, owner_id)


@router.get("/mcp-servers")
def configured_mcp_servers() -> dict:
    return {
        "servers": studio_settings.public(owner_id=current_account().id).mcp_configs
    }


@router.post("/mcp-servers/test")
def test_mcp_servers(req: MCPServersRequest) -> dict:
    try:
        entries = normalize_mcp_payload(req.config)
        entries = merge_masked_mcp_configs(
            entries,
            studio_settings.load(current_account().id).mcp_configs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _, results = probe_mcp_configs(entries)
    return {"servers": results}


@router.post("/mcp-servers", response_model=StudioSettings)
def save_mcp_servers(req: MCPServersRequest) -> StudioSettings:
    try:
        entries = normalize_mcp_payload(req.config)
        owner_id = current_account().id
        entries = merge_masked_mcp_configs(
            entries,
            studio_settings.load(owner_id).mcp_configs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    prepared, results = probe_mcp_configs(entries)
    failures = [item for item in results if item.get("status") != "connected"]
    if failures:
        raise HTTPException(status_code=422, detail={"servers": results})
    return studio_settings.public(
        studio_settings.upsert_mcp_configs(prepared, owner_id),
        owner_id,
    )


@router.patch("/mcp-servers/{name}", response_model=StudioSettings)
def update_mcp_server(name: str, req: UpdateMCPServerRequest) -> StudioSettings:
    owner_id = current_account().id
    updated = studio_settings.set_mcp_enabled(name, req.enabled, owner_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="MCP server not found.")
    return studio_settings.public(updated, owner_id)


@router.delete("/mcp-servers/{name}", response_model=StudioSettings)
def delete_mcp_server(name: str) -> StudioSettings:
    owner_id = current_account().id
    updated = studio_settings.delete_mcp_config(name, owner_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="MCP server not found.")
    return studio_settings.public(updated, owner_id)


def _installed_skill_response(skill, owner_id: str) -> dict:
    current = studio_settings.load(owner_id)
    if skill.name not in current.installed_skills:
        current.installed_skills.append(skill.name)
    saved = studio_settings.save(current, owner_id)
    return {
        "skill": skill.model_dump(),
        "settings": studio_settings.public(saved, owner_id).model_dump(mode="json"),
    }


def _require_skill_install_consent(value: str | None) -> None:
    if (value or "").strip().casefold() != "true":
        raise HTTPException(
            status_code=403,
            detail="Skill installation requires X-Studio-Install-Consent: true.",
        )
