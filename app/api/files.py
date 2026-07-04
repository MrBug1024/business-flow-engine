"""文件上传与表结构接口（v1.0.3：上传时即标角色）。

变更：上传接口接受 `roles` 表单字段（与 files 一一对应；input/rule/result），
把"选择文件 → 标角色 → 上传"合并为单个动作，减少用户步骤。
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile

from .. import table_io, transform_builder
from ..models import Scenario, ScenarioStatus, TableMeta, TableRoleRequest, TableRole
from ..storage import store
from .deps import get_owned_scenario_or_404

router = APIRouter(tags=["files"])

_ALLOWED_SUFFIX = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".md", ".markdown"}
_VALID_ROLES = {TableRole.INPUT.value, TableRole.RULE.value, TableRole.RESULT.value}


@router.post("/scenarios/{scenario_id}/uploads")
async def upload_tables(
    files: list[UploadFile],
    roles: Optional[str] = Form(default=None),
    scenario: Scenario = Depends(get_owned_scenario_or_404),
) -> dict:
    """上传一个或多个文件，并按 roles（input/rule/result）即时标注角色。

    roles 形式（任选其一）：
      * JSON 数组（与 files 顺序一致）：`["input", "rule", "result"]`
      * JSON 对象（按文件名映射）：`{"order.csv": "input", "rule.xlsx": "rule"}`
      * 逗号分隔：`input,rule,result`
    缺省时角色为 unknown，需在前端"表格"区补标。
    """
    uploads_dir = store.uploads_dir(scenario.id)
    role_map = _parse_roles(roles, [f.filename for f in files])

    new_metas: list[TableMeta] = []
    existing = {t.table_name: t for t in scenario.tables_meta}

    for upload in files:
        suffix = "." + upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
        if suffix not in _ALLOWED_SUFFIX:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型：{upload.filename}（支持 CSV/TSV/Excel/JSON/Markdown）",
            )
        dest = uploads_dir / upload.filename
        dest.write_bytes(await upload.read())
        try:
            meta = table_io.inspect_table(str(dest))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"解析 {upload.filename} 失败：{exc}")

        # 应用上传时选择的角色（按文件名映射或按位置）
        chosen = role_map.get(upload.filename) or role_map.get(meta.table_name)
        if chosen and chosen in _VALID_ROLES:
            meta.role = chosen
            meta.role_confirmed = True
        else:
            prev = existing.get(meta.table_name)
            if prev and prev.role_confirmed:
                meta.role = prev.role
                meta.role_confirmed = True

        existing[meta.table_name] = meta
        new_metas.append(meta)

    scenario.tables_meta = list(existing.values())
    if scenario.status == ScenarioStatus.CREATED:
        scenario.status = ScenarioStatus.TABLES_UPLOADED
    store.save(scenario)

    return {
        "message": f"成功上传 {len(new_metas)} 个文件",
        "tables_meta": [m.model_dump() for m in new_metas],
        "status": scenario.status.value,
    }


def _parse_roles(roles: str | None, file_names: list[str]) -> dict[str, str]:
    """解析 roles 参数为 {文件名: 角色} 映射（兼容多种表单格式）。"""
    if not roles:
        return {}
    raw = roles.strip()
    out: dict[str, str] = {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            for i, role in enumerate(parsed):
                if i < len(file_names) and isinstance(role, str):
                    out[file_names[i]] = role.strip().lower()
            return out
        if isinstance(parsed, dict):
            for name, role in parsed.items():
                if isinstance(role, str):
                    out[name] = role.strip().lower()
            return out
    except json.JSONDecodeError:
        pass
    # 逗号分隔的回退
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    for i, role in enumerate(parts):
        if i < len(file_names):
            out[file_names[i]] = role
    return out


@router.get("/scenarios/{scenario_id}/tables", response_model=list[TableMeta])
def list_tables(scenario: Scenario = Depends(get_owned_scenario_or_404)) -> list[TableMeta]:
    return scenario.tables_meta


@router.put("/scenarios/{scenario_id}/tables/{table_name}/role", response_model=TableMeta)
def set_table_role(
    table_name: str, req: TableRoleRequest,
    scenario: Scenario = Depends(get_owned_scenario_or_404),
) -> TableMeta:
    """事后修正某张表的角色（上传时已选，这里仅作修正用）。"""
    meta = next((t for t in scenario.tables_meta if t.table_name == table_name), None)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"未找到表：{table_name}")
    meta.role = req.role
    meta.role_confirmed = req.role != TableRole.UNKNOWN.value
    domain = transform_builder.build_domain_knowledge(scenario)
    scenario.domain_knowledge = domain
    if scenario.flow or any(t.role == TableRole.RESULT.value for t in scenario.tables_meta):
        scenario.outputs = transform_builder.build_outputs(scenario, domain)
    store.save(scenario)
    return meta
