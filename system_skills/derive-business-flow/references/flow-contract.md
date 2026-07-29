# 业务流程候选结构契约

## 顶层结构

候选文件使用以下结构。字段名固定，说明文字按当前业务场景填写。

~~~json
{
  "schema_version": 1,
  "source": {
    "capability": "discover-data-relations",
    "artifact": "/workspace/outputs/data-relations/scenario-relationship.json",
    "fingerprint": "由 prepare 写入的 SHA-256"
  },
  "scenario": {
    "name": "场景名称",
    "purpose": "场景范围",
    "business_outcome": "整个场景最终形成的业务结果",
    "grain": "macro_business_scenario"
  },
  "history_policy": {
    "role": "validation_only",
    "statement": "历史数据仅用于验证，不用于定义标准流程。"
  },
  "stages": [],
  "transitions": [],
  "main_flow": [],
  "states": [],
  "controls": [],
  "validation_checks": [],
  "open_questions": [],
  "coverage": {}
}
~~~

不要手写或更改 `source.artifact` 与 `source.fingerprint`；从 `flow-claims.template.json` 原样保留。

## 阶段

~~~json
{
  "id": "s_assess",
  "name": "执行规则驱动判定",
  "stage_type": "decision",
  "objective": "基于已汇聚的业务上下文和适用规则形成场景级判断",
  "outcome": "形成可供结果落实使用的判定结论",
  "owner_role": "可留空；只有上游 actor 支撑时才填写",
  "input_node_ids": ["n_context", "n_rules"],
  "output_node_ids": ["n_decision"],
  "support": {
    "upstream_node_ids": ["n_context", "n_rules", "n_decision"],
    "upstream_edge_ids": ["e_context_feeds_decision", "e_rules_govern_decision"],
    "evidence_ids": ["E-...", "E-..."]
  },
  "inference": {
    "basis": "structural",
    "confidence": 0.86,
    "rationale": "上游已验收关系表明业务上下文汇入判定且规则约束判定，因此综合为一个宏观判断责任。"
  }
}
~~~

`stage_type` 允许：

- `initiation`
- `preparation`
- `processing`
- `decision`
- `fulfillment`
- `closure`
- `oversight`

`main_flow` 至少三个阶段，不能重复，最后一个阶段必须是 `fulfillment` 或 `closure`。

## 流转

~~~json
{
  "id": "t_assess_to_result",
  "source": "s_assess",
  "target": "s_result",
  "type": "normal",
  "label": "判定结论进入结果形成",
  "condition": "",
  "support": {
    "upstream_node_ids": ["n_decision", "n_result"],
    "upstream_edge_ids": ["e_decision_derives_result"],
    "evidence_ids": ["E-..."]
  },
  "inference": {
    "basis": "structural",
    "confidence": 0.9,
    "rationale": "上游 derives 关系明确判定形成最终结果，可支撑宏观责任的先后。"
  }
}
~~~

`type` 允许 `normal`、`handoff`、`conditional`、`exception`、`return`。主流程相邻阶段必须有同方向的 `normal` 或 `handoff`。

`explicit` 流转必须引用明确时序关系；`structural` 流转必须引用方向性关系。条件与异常分支必须额外引用规则/判定；返回必须引用 `returns_to`。

## 状态

~~~json
{
  "id": "st_completed",
  "name": "业务结果已形成",
  "state_type": "terminal",
  "reached_after": "s_result",
  "meaning": "场景级结果已经生成，可进入后续使用或反馈",
  "support": {
    "upstream_node_ids": ["n_result"],
    "upstream_edge_ids": ["e_decision_derives_result"],
    "evidence_ids": ["E-..."]
  },
  "inference": {
    "basis": "structural",
    "confidence": 0.9,
    "rationale": "上游输出节点和结果形成关系共同支撑该终态。"
  }
}
~~~

`state_type` 允许 `entry`、`intermediate`、`terminal`、`exception`。至少一个 `terminal`。

## 控制

~~~json
{
  "id": "c_rules",
  "name": "业务判定规则",
  "applies_to": ["s_assess"],
  "policy": "在判定阶段约束结论形成",
  "support": {
    "upstream_node_ids": ["n_rules"],
    "upstream_edge_ids": ["e_rules_govern_decision"],
    "evidence_ids": ["E-..."]
  }
}
~~~

控制必须由上游 `rule` 节点或 `governs`/`governed_by` 关系支撑。

## 历史数据验证检查

~~~json
{
  "id": "v_result_trace",
  "target_kind": "state",
  "target_id": "st_completed",
  "method": "input_output_traceability",
  "role": "validate_not_define",
  "question": "历史结果能否追溯到形成判定所需的业务输入？",
  "pass_signal": "抽样结果均能通过上游已验收关联追溯到对应输入域",
  "source_node_ids": ["n_context", "n_result"],
  "limitation": "只验证可追溯性，不证明流程不存在人工例外。"
}
~~~

验证方法允许：`sequence_consistency`、`state_coverage`、`input_output_traceability`、`branch_frequency`、`outcome_reconciliation`、`control_conformance`。

## 待确认项

~~~json
{
  "id": "q_manual_review",
  "question": "低置信度判定是否必须进入人工复核？",
  "impact": "决定是否增加异常分支",
  "related_stage_ids": ["s_assess"]
}
~~~

待确认项不进入阶段或流转，不影响候选在已证实范围内完成。

## 覆盖

~~~json
{
  "used_upstream_node_ids": ["n_context", "n_rules", "n_decision", "n_result"],
  "used_upstream_edge_ids": ["e_context_feeds_decision", "e_rules_govern_decision", "e_decision_derives_result"],
  "context_only": [
    {
      "kind": "edge",
      "id": "e_context_joins_reference",
      "reason": "只证明跨材料可追溯，不定义业务阶段或顺序"
    }
  ],
  "inventory_upstream_node_ids": [],
  "inventory_upstream_edge_ids": []
}
~~~

`used_upstream_*` 必须精确等于候选其他部分实际引用的上游 ID。每个未使用上游节点或关系必须在 `context_only` 中说明原因；同一 ID 不能同时出现在两边。

