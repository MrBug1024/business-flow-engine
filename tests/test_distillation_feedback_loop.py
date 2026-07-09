import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import scenarios as scenarios_api
from app.distillation import chat_service, clarifications, trace_sampling
from app.distillation.chat_service import (
    _should_use_deterministic_workflow,
    _step_interaction,
)
from app.domain import scenario_state
from app.domain.models import (
    ColumnMeta,
    CreateScenarioRequest,
    Relation,
    RelationResult,
    Scenario,
    TableMeta,
    TableRole,
)


def _write_csv(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _table(path: str, name: str, role: str, columns: list[str]) -> TableMeta:
    return TableMeta(
        table_name=name,
        display_name=name,
        file_path=path,
        role=role,
        role_confirmed=True,
        row_count=2,
        col_count=len(columns),
        columns=[ColumnMeta(name=c, dtype="object") for c in columns],
    )


def test_business_trace_does_not_use_unrelated_value_scan(tmp_path: Path):
    result_path = _write_csv(tmp_path / "result.csv", "case_id,amount\nR-001,10\n")
    business_path = _write_csv(
        tmp_path / "business.csv",
        "row_id,note\nB-1,R-001\nB-2,other\n",
    )
    scenario = Scenario(
        id="sc_test",
        name="trace test",
        tables_meta=[
            _table(result_path, "result", TableRole.RESULT.value, ["case_id", "amount"]),
            _table(business_path, "business", TableRole.INPUT.value, ["row_id", "note"]),
        ],
    )

    report = trace_sampling.trace_sampling(scenario)
    info = report["trace_map"]["business"]

    assert info["matched_rows"] == []
    assert "business" in report["unmatched_tables"]


def test_confirmed_relation_drives_trace_even_when_column_names_differ(tmp_path: Path):
    result_path = _write_csv(tmp_path / "result.csv", "case_id,amount\nR-001,10\n")
    business_path = _write_csv(
        tmp_path / "business.csv",
        "row_id,source_key\nB-1,R-001\nB-2,other\n",
    )
    scenario = Scenario(
        id="sc_test",
        name="trace test",
        tables_meta=[
            _table(result_path, "result", TableRole.RESULT.value, ["case_id", "amount"]),
            _table(business_path, "business", TableRole.INPUT.value, ["row_id", "source_key"]),
        ],
        relations=RelationResult(relations=[
            Relation(
                from_table="result",
                from_column="case_id",
                to_table="business",
                to_column="source_key",
                confidence=1.0,
                confirmed=True,
            )
        ]),
    )

    report = trace_sampling.trace_sampling(scenario)
    info = report["trace_map"]["business"]

    assert info["matched_source"] == "confirmed"
    assert info["matched_rows"][0]["source_key"] == "R-001"


def test_retrace_preserves_confirmed_relations():
    confirmed = Relation(
        from_table="result",
        from_column="case_id",
        to_table="business",
        to_column="source_key",
        confidence=1.0,
        confirmed=True,
    )
    weak = Relation(
        from_table="result",
        from_column="amount",
        to_table="business",
        to_column="row_id",
        confidence=0.5,
        confirmed=False,
    )
    scenario = Scenario(
        id="sc_test",
        name="state test",
        relations=RelationResult(relations=[confirmed, weak]),
        trace_chain={"result_sample": [{"case_id": "R-001"}]},
    )

    scenario_state.invalidate_after_trace(scenario)

    assert scenario.relations is not None
    assert scenario.relations.relations == [confirmed]
    assert scenario.relations.trace_chain == scenario.trace_chain


def test_confirmation_text_falls_back_to_structured_clarification():
    items = clarifications.build_clarifications_from_text(
        "请确认这些表是否参与本次业务流程？如果参与，请补充它们通过哪个业务编号连接。",
        context="test",
    )

    assert len(items) == 1
    assert items[0].allow_custom is True
    assert items[0].options


def test_step_interaction_uses_ambiguous_questions_without_shadowing_module():
    result = SimpleNamespace(
        clarifications=[],
        ambiguous_questions=["请确认这些表是否参与本次业务流程？"],
    )

    interaction = _step_interaction("deduce_relations", result)

    assert interaction is not None
    assert interaction["questions"][0]["allow_custom"] is True


def test_explicit_workflow_commands_bypass_llm_agent_decision():
    assert _should_use_deterministic_workflow("请进行数据链路追踪")
    assert _should_use_deterministic_workflow("执行步骤 3：推导关联关系")
    assert _should_use_deterministic_workflow("重新推导 关联关系")
    assert _should_use_deterministic_workflow("步骤三重来")
    assert _should_use_deterministic_workflow("请推导业务流程")
    assert _should_use_deterministic_workflow("请生成技能")
    assert _should_use_deterministic_workflow("deduce_relations")
    assert not _should_use_deterministic_workflow("这个字段应该关联到订单编号")
    assert not _should_use_deterministic_workflow("你好，帮我看看当前状态")


def test_stream_chat_emits_error_and_done_when_step_crashes(monkeypatch):
    async def broken_stream(*_args, **_kwargs):
        raise RuntimeError("boom")
        yield ""  # pragma: no cover

    async def collect():
        scenario = Scenario(id="sc_broken", name="broken")
        return [
            frame
            async for frame in chat_service.stream_chat(scenario, "请推导关联关系")
        ]

    monkeypatch.setattr(chat_service, "_stream_heuristic", broken_stream)
    monkeypatch.setattr(chat_service.store, "append_message", lambda *_args, **_kwargs: None)

    frames = asyncio.run(collect())

    assert any('"type": "error"' in frame and "boom" in frame for frame in frames)
    assert '"type": "done"' in frames[-1]


def test_create_scenario_requires_nonblank_business_description(monkeypatch):
    monkeypatch.setattr(
        scenarios_api.store,
        "create",
        lambda **_kwargs: pytest.fail("empty business description should not create"),
    )

    with pytest.raises(HTTPException) as exc:
        scenarios_api.create_scenario(
            CreateScenarioRequest(name="医保审计", description="   "),
            SimpleNamespace(id="u_test"),
        )

    assert exc.value.status_code == 400
