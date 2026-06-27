"""文件上传与表结构接口（专用 REST）。

上传后立即轻量扫描每张表（仅表头 + 随机样本），更新场景的表结构元信息。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile

from .. import table_io
from ..models import ScenarioStatus, TableMeta
from ..storage import store
from .deps import get_scenario_or_404

router = APIRouter(tags=["files"])

# 允许上传的表格类型
_ALLOWED_SUFFIX = {".csv", ".tsv", ".xlsx", ".xls"}


@router.post("/scenarios/{scenario_id}/uploads")
async def upload_tables(scenario_id: str, files: list[UploadFile]) -> dict:
    """上传一个或多个业务数据表。"""
    scenario = get_scenario_or_404(scenario_id)
    uploads_dir = store.uploads_dir(scenario_id)

    new_metas: list[TableMeta] = []
    existing = {t.table_name: t for t in scenario.tables_meta}

    for upload in files:
        suffix = "." + upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
        if suffix not in _ALLOWED_SUFFIX:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型：{upload.filename}（仅支持 CSV/TSV/Excel）",
            )
        dest = uploads_dir / upload.filename
        dest.write_bytes(await upload.read())
        try:
            meta = table_io.inspect_table(str(dest))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"解析 {upload.filename} 失败：{exc}")
        existing[meta.table_name] = meta  # 同名覆盖
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


@router.get("/scenarios/{scenario_id}/tables", response_model=list[TableMeta])
def list_tables(scenario_id: str) -> list[TableMeta]:
    """获取业务场景下的所有表结构元信息。"""
    return get_scenario_or_404(scenario_id).tables_meta
