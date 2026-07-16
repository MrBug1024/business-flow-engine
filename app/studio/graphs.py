"""Graph builders for Business Context visualizations."""

from __future__ import annotations

import re
from typing import Any

from app.studio.models import BusinessContext


def entity_graph(ctx: BusinessContext) -> dict[str, Any]:
    nodes = [
        {"id": item["id"], "label": item["name"], "type": item.get("type", "entity")}
        for item in ctx.entities
    ]
    edges = [
        {
            "source": item.get("source"),
            "target": item.get("target"),
            "label": item.get("label", item.get("type", "related")),
            "confidence": item.get("confidence", 0.6),
        }
        for item in ctx.relations
    ]
    lines = ["flowchart LR"]
    if not nodes:
        lines.append('    empty["等待业务资料或 AI 分析"]')
    for node in nodes:
        lines.append(f'    {_node_id(node["id"])}["{_escape(node["label"])}"]')
    for edge in edges:
        if edge["source"] and edge["target"]:
            lines.append(
                f'    {_node_id(edge["source"])} -- "{_escape(edge["label"])}" --> {_node_id(edge["target"])}'
            )
    return {"kind": "entity", "nodes": nodes, "edges": edges, "mermaid": "\n".join(lines)}


def flow_graph(ctx: BusinessContext) -> dict[str, Any]:
    open_questions = [item for item in ctx.questions if item.get("status", "open") == "open"]
    steps = [
        {"id": "start", "label": "创建业务场景"},
        {"id": "files", "label": "上传 / 描述业务资料"},
        {"id": "analyze", "label": "AI 分析并更新 Context"},
        {"id": "confirm", "label": "用户确认关键假设" if open_questions else "确认信息已沉淀"},
        {"id": "outputs", "label": "生成 Prompt 与 Skill Package"},
    ]
    if ctx.flows:
        steps.extend(ctx.flows)
    lines = ["flowchart TD"]
    for step in steps:
        lines.append(f'    {_node_id(step["id"])}["{_escape(step.get("label", step.get("name", "")))}"]')
    for left, right in zip(steps, steps[1:]):
        lines.append(f'    {_node_id(left["id"])} --> {_node_id(right["id"])}')
    return {"kind": "flow", "steps": steps, "mermaid": "\n".join(lines)}


def lineage_graph(ctx: BusinessContext) -> dict[str, Any]:
    lines = ["flowchart LR", '    context["Business Context"]', '    outputs["Prompt / Skill Package"]']
    edges: list[dict[str, Any]] = []
    for item in ctx.data_lineage:
        source_id = _node_id(item["id"] + "_source")
        label = item.get("source", "source")
        lines.append(f'    {source_id}["{_escape(label)}"]')
        lines.append(f"    {source_id} --> context")
        edges.append({"source": label, "target": "Business Context", "operation": item.get("operation")})
    lines.append("    context --> outputs")
    edges.append({"source": "Business Context", "target": "Prompt / Skill Package", "operation": "generate_outputs"})
    return {"kind": "lineage", "items": ctx.data_lineage, "edges": edges, "mermaid": "\n".join(lines)}


def evidence_graph(ctx: BusinessContext) -> dict[str, Any]:
    lines = ["flowchart TD", '    conclusion["当前业务理解"]']
    for item in ctx.evidence[:12]:
        node = _node_id(item["id"])
        label = f"{item.get('source', '证据')}\\n{item.get('confidence', 0.6):.0%}"
        lines.append(f'    {node}["{_escape(label)}"]')
        lines.append(f"    {node} --> conclusion")
    if not ctx.evidence:
        lines.append('    empty["暂无证据，需上传资料或补充描述"] --> conclusion')
    return {"kind": "evidence", "evidence": ctx.evidence, "mermaid": "\n".join(lines)}


def _node_id(raw: str) -> str:
    return "n_" + re.sub(r"[^a-zA-Z0-9_]+", "_", raw)


def _escape(raw: str) -> str:
    return str(raw).replace('"', "'").replace("\n", "<br/>")

