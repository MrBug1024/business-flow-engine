"""图谱、产出与技能接口（v1.0.3：移除 knowledge_library 端点；新增 rule-schema）。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from .. import executor, trace_sampling, validators
from ..models import (
    EvolveSkillRequest,
    FlowResult,
    ProduceRequest,
    Relation,
    RelationConfirmRequest,
    RelationResult,
    RuleSchemaMapping,
    Skill,
    ValidationReport,
)
from ..skill_builder import materialize_evolved_skill
from ..storage import store
from ..validators import validate_trace_connectivity
from .deps import get_owned_scenario_or_404, get_scenario_or_404

# 本路由下所有端点都是 /scenarios/{scenario_id}/...，统一在路由级强制「登录 + 归属校验」，
# 处理函数内部再用 get_scenario_or_404 取对象（此时已确认归属，仅读文件）。
router = APIRouter(tags=["graph"], dependencies=[Depends(get_owned_scenario_or_404)])


@router.get("/scenarios/{scenario_id}/trace-sample")
def get_trace_sample(scenario_id: str, result_table: Optional[str] = None) -> dict:
    """暴露「追踪驱动采样」的真实计算结果，供前端逐表核对：

    推导关联/流程时喂给 AI 的样本，到底是不是「结果表第 N 行 → 业务表对应行 →
    规则表对应行」这样有真实因果链路的数据，还是退化成了各表独立随机抽的、
    互相之间毫无关系的行。

    返回结构（与 trace_sampling.trace_sampling() 一致）：
        result_table / result_sample：追踪入口（结果表样本行）
        trace_map：{表名: {matched_rows, matched_by, trace_confidence, warning?}}
            matched_by == "random" 即表示这张表没追到任何因果关联，是随机兜底
        unmatched_tables：完全追不上的表
        trace_summary：一句话摘要
        degraded：True 表示整体降级为随机采样（通常因为没有结果表）
    """
    scenario = get_scenario_or_404(scenario_id)
    if not scenario.tables_meta:
        return {"degraded": True, "trace_summary": "尚未上传任何表", "trace_map": {}, "result_sample": []}
    report = trace_sampling.trace_sampling(scenario, result_table_name=result_table or None)
    check = validate_trace_connectivity(report)
    report["connectivity_check"] = check.to_dict()
    return report


@router.get("/scenarios/{scenario_id}/relations", response_model=RelationResult)
def get_relations(scenario_id: str) -> RelationResult:
    """关联关系 + 字段语义（合并返回）。"""
    scenario = get_scenario_or_404(scenario_id)
    return scenario.relations or RelationResult(summary="尚未推导关联关系。")


def _match_relation(r: Relation, req: RelationConfirmRequest) -> bool:
    from_cols = tuple(r.from_columns or [r.from_column])
    to_cols = tuple(r.to_columns or [r.to_column])
    req_from = tuple(req.from_columns or [req.from_column])
    req_to = tuple(req.to_columns or [req.to_column])
    return (r.from_table, from_cols, r.to_table, to_cols) == (req.from_table, req_from, req.to_table, req_to)


@router.post("/scenarios/{scenario_id}/relations/confirm", response_model=RelationResult)
def confirm_relation(scenario_id: str, req: RelationConfirmRequest) -> RelationResult:
    """人工确认一条关联关系（已存在的关联提升为「已确认」；不存在则新增一条）。

    确认后置信度固定为 1.0，且后续「推导关联关系」重新跑一遍也不会把它覆盖/丢弃
    ——人工核实过值确实相等，就不该再让 AI 每次重新判断一遍。确认后会强制用这条
    关联重新在真实数据上搜一遍因果链并覆盖保存，让「推导业务流程」直接用上修正结果。
    """
    scenario = get_scenario_or_404(scenario_id)
    known_tables = {t.table_name for t in scenario.tables_meta}
    if req.from_table not in known_tables or req.to_table not in known_tables:
        raise HTTPException(status_code=400, detail="表名不存在于当前场景中。")

    if scenario.relations is None:
        scenario.relations = RelationResult()

    validators.upsert_confirmed_relation(
        scenario.relations, req.from_table, req.from_column, req.to_table, req.to_column,
        from_columns=req.from_columns, to_columns=req.to_columns, relation_type=req.relation_type,
    )

    try:
        scenario.relations.trace_chain = trace_sampling.trace_sampling(scenario)
    except Exception:  # noqa: BLE001
        pass

    store.save(scenario)
    return scenario.relations


@router.delete("/scenarios/{scenario_id}/relations/confirm", response_model=RelationResult)
def unconfirm_relation(scenario_id: str, req: RelationConfirmRequest) -> RelationResult:
    """取消人工确认（关联本身不删除，只是不再豁免被下一轮推导覆盖）。"""
    scenario = get_scenario_or_404(scenario_id)
    if scenario.relations:
        for r in scenario.relations.relations:
            if _match_relation(r, req):
                r.confirmed = False
        store.save(scenario)
    return scenario.relations or RelationResult()


@router.get("/scenarios/{scenario_id}/flow", response_model=FlowResult)
def get_flow(scenario_id: str) -> FlowResult:
    """业务流程（含节点能力描述 + 规则结构映射 + Mermaid）。"""
    scenario = get_scenario_or_404(scenario_id)
    return scenario.flow or FlowResult(summary="尚未推导业务流程。")


@router.get("/scenarios/{scenario_id}/rule-schema")
def get_rule_schema(scenario_id: str) -> Optional[RuleSchemaMapping]:
    """规则表结构映射（discriminator → template）。"""
    scenario = get_scenario_or_404(scenario_id)
    return scenario.flow.rule_schema if scenario.flow else None


@router.get("/scenarios/{scenario_id}/outputs")
def get_outputs(scenario_id: str) -> dict:
    scenario = get_scenario_or_404(scenario_id)
    validated = {v.output_id for v in scenario.validations if v.passed}
    items = []
    for o in scenario.outputs:
        items.append({
            "output_id": o.output_id, "name": o.name, "fmt": o.fmt,
            "status": ("verified" if o.output_id in validated else o.status),
            "strategy": o.strategy, "columns": o.columns, "params": o.params,
            "required_tables": o.required_tables, "result_table": o.result_table,
            "has_sql": bool(o.sql), "match_rate": o.match_rate,
            "external_data_needed": o.external_data_needed,
            "pipeline_steps": len(o.pipeline),
        })
    return {"outputs": items, "count": len(items)}


@router.get("/scenarios/{scenario_id}/domain-knowledge")
def get_domain_knowledge(scenario_id: str) -> dict:
    scenario = get_scenario_or_404(scenario_id)
    if scenario.domain_knowledge is None:
        return {"scenario": scenario.name, "tables": [], "relations": [], "result_schema": {}}
    return scenario.domain_knowledge.model_dump()


@router.get("/scenarios/{scenario_id}/validations", response_model=list[ValidationReport])
def get_validations(scenario_id: str) -> list[ValidationReport]:
    return get_scenario_or_404(scenario_id).validations


@router.post("/scenarios/{scenario_id}/produce", response_model=ValidationReport)
def produce_output(scenario_id: str, req: ProduceRequest) -> ValidationReport:
    """对指定产出执行 + 复刻 + 对照（REST 直调）。

    若已生成主技能 + 提供了 rule_filter，**优先走主技能的参数化引擎**：
        - rule_filter=None：应用全部规则
        - rule_filter="X射线"：关键词匹配
        - rule_filter={"序号":"232"}：精确序号
    否则走老的 pipeline executor。
    """
    scenario = get_scenario_or_404(scenario_id)
    o = next((x for x in scenario.outputs if x.output_id == req.output_id.strip()), None)
    if o is None:
        raise HTTPException(status_code=404, detail=f"未知产出：{req.output_id}")

    # 优先走参数化主技能（兼容新名 main_skill 和旧名 business_executor）
    _skills_base = store.skills_dir(scenario_id)
    skill_file = None
    for _sname in ("main_skill", "business_executor"):
        _cand = _skills_base / _sname / "scripts" / "skill_executor.py"
        if _cand.exists():
            skill_file = _cand
            break
    if skill_file is not None and scenario.skills:
        try:
            import importlib.util
            spec_mod = importlib.util.spec_from_file_location("_se_api", str(skill_file))
            mod = importlib.util.module_from_spec(spec_mod)
            spec_mod.loader.exec_module(mod)
            data_dir = str(store.uploads_dir(scenario_id))
            out_dir = store.outputs_dir(scenario_id)
            result = mod.produce(req.output_id, data_dir, out_dir=str(out_dir),
                                  params=req.rule_filter, max_rows=20000)
            applied = result.get("applied", result.get("applied_rules", 0))
            report = ValidationReport(
                output_id=o.output_id, output_name=o.name,
                produced_count=result.get("rows", 0),
                artifact_path=result.get("artifact", ""),
                artifact_url=(
                    f"/api/scenarios/{scenario_id}/outputs/files/{Path(result['artifact']).name}"
                    if result.get("artifact") else ""
                ),
                passed=result.get("rows", 0) > 0,
                message=(f"参数化主技能执行：params={req.rule_filter!r}，"
                         f"应用 {applied} 条知识条目，"
                         f"命中 {result.get('rows', 0)} 行。"),
            )
            scenario.validations = [v for v in scenario.validations
                                    if v.output_id != req.output_id] + [report]
            store.save(scenario)
            return report
        except Exception as exc:  # noqa: BLE001
            # 回退到老 pipeline executor
            pass

    out_dir = store.outputs_dir(scenario_id)
    report = executor.execute_and_compare(scenario, o, req.data_sources or None, out_dir=out_dir)
    if report.artifact_path:
        report.artifact_url = (
            f"/api/scenarios/{scenario_id}/outputs/files/{Path(report.artifact_path).name}"
        )
    o.match_rate = report.match_rate
    if report.passed:
        o.status = "verified"
    scenario.validations = [v for v in scenario.validations
                            if v.output_id != req.output_id] + [report]
    store.save(scenario)
    return report


@router.get("/scenarios/{scenario_id}/outputs/files/{filename}")
def download_output_file(scenario_id: str, filename: str) -> FileResponse:
    get_scenario_or_404(scenario_id)
    safe = Path(filename).name
    path = store.outputs_dir(scenario_id) / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"产出文件不存在：{safe}")
    return FileResponse(path, filename=safe)


@router.get("/scenarios/{scenario_id}/skills", response_model=list[Skill])
def get_skills(scenario_id: str) -> list[Skill]:
    return get_scenario_or_404(scenario_id).skills


@router.post("/scenarios/{scenario_id}/skills/evolve", response_model=Skill, status_code=201)
def evolve_skill(scenario_id: str, req: EvolveSkillRequest) -> Skill:
    scenario = get_scenario_or_404(scenario_id)
    if not scenario.skills:
        raise HTTPException(status_code=400, detail="请先生成基础技能库，再添加进化技能。")
    skill = Skill(
        skill_id=f"skill_evolved_{uuid.uuid4().hex[:8]}",
        name=req.name.strip(),
        operation="EVOLVED",
        description=req.description.strip(),
        is_evolved=True,
        status="evolved",
    )
    skill = materialize_evolved_skill(scenario, skill)
    scenario.skills.append(skill)
    store.save(scenario)
    return skill
