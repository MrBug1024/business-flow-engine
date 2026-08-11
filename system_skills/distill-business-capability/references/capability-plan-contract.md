# 能力计划候选契约

候选以 `capability-plan.template.json` 为基础。模板已经固化上游事实，Agent 只填写空白业务语义字段。

## 顶层

~~~json
{
  "schema_version": 1,
  "generator_contract_version": 2,
  "source": {
    "relations": {"capability": "discover-data-relations", "artifact": "...", "fingerprint": "..."},
    "flow": {"capability": "derive-business-flow", "artifact": "...", "fingerprint": "..."},
    "operational_data_contract": {"capability": "discover-data-relations", "artifact": "...", "fingerprint": "..."}
  },
  "scenario": {},
  "bundle": {
    "name": "portable-scenario-capabilities",
    "description": "说明场景、能力范围和第三方 Agent 用途",
    "target_agents": "third_party_agents"
  },
  "portability": {
    "platform_independent": true,
    "python_requirement": ">=3.10",
    "resource_paths": "relative_to_each_skill",
  "credentials": "preserve_public_defaults_externalize_credentials"
  },
  "file_inventory": [],
  "foundation_skills": [],
  "stage_skills": [],
  "orchestrator": {},
  "unsupported_formats": []
}
~~~

`generator_contract_version`、`source`、`scenario`、`portability`、`file_inventory` 以及各阶段 `execution_contract` 不得改写。版本不等于当前模板时必须重新运行 `prepare`，不能复用旧候选。`file_inventory` 中的 `lifecycle`、`runtime_required` 和 `runtime_binding` 由上游契约固化：结果模板不因为存在文件记录就成为运行时输入。

## 基础 Skill

~~~json
{
  "id": "foundation-tabular",
  "kind": "tabular",
  "skill_name": "scenario-tabular-reader",
  "display_name": "场景表格数据读取",
  "description": "说明当前场景、真实格式、业务文件角色和何时调用",
  "formats": [".csv", ".xlsx"],
  "engine": "duckdb",
  "source_template": "portable-tabular-reader",
  "file_roles": [],
  "when_to_use": ["至少两个具体触发条件"],
  "scenario_instructions": ["至少一条与业务文件角色相关的指令"],
  "non_goals": ["不作业务判断"]
}
~~~

只填写 `skill_name`、`display_name`、`description`、`when_to_use`、`scenario_instructions` 和 `non_goals`。其他字段由模板决定。

同理：

- `foundation-document` 使用 `portable-document-extractors`；
- `foundation-ocr` 使用 `httpx-ocr-client` 和 `adapted-ocr-parser`。
- `foundation-knowledge` 使用 `vector-kb-http-client` 和 `adapted-vector-kb`；其 `system_roles` 由明确的外部知识关系节点生成，不得改写。外部能力生命周期为 `optional_enrichment`，是否必需由 Agent 根据用户请求和完整规则记录判断。

## 阶段 Skill

~~~json
{
  "id": "cap-s_assess",
  "stage_id": "s_assess",
  "skill_name": "scenario-assess-request",
  "display_name": "执行场景判定",
  "description": "说明何时因上游输入或用户请求调用，并说明形成什么结果",
  "objective": "模板给定",
  "outcome": "模板给定",
  "invocation_triggers": ["用户请求...", "上游阶段已形成..."],
  "foundation_ids": ["foundation-tabular"],
  "predecessor_stage_ids": ["s_prepare"],
  "successor_stage_ids": ["s_result"],
  "input_contract": [],
  "output_contract": [],
  "control_ids": ["c_rules"],
  "procedure": [
    {
      "action": "验证并读取阶段所需业务输入，不扩展输入范围",
      "basis": "input_contract",
      "source_ids": ["n_context"]
    },
    {
      "action": "依据已验收控制形成阶段结果并按输出契约交付",
      "basis": "control",
      "source_ids": ["c_rules", "n_decision"]
    }
  ],
  "non_goals": ["不完成后续结果落实阶段"],
  "open_question_ids": []
}
~~~

只填写 `skill_name`、必要时优化 `display_name`、`description`、`invocation_triggers`、`procedure` 和 `non_goals`。其余字段不得改变。

所有 `input_contract[].required` 与 `output_contract[].required` 都由模板固定为 `true`。阶段 Skill 必须形成其已验收 outcome，Agent 不得把正式阶段输入或输出降级为可选。

procedure 的 `basis` 允许：

- `flow_stage`
- `input_contract`
- `control`
- `handoff`

`source_ids` 只能引用本阶段、输入输出关系节点、适用控制、相关状态、相邻 transition 或本阶段待确认项。待确认项不能作为正式执行依据，只能在 Skill 边界中展示。

## 场景总控

~~~json
{
  "id": "scenario-orchestrator",
  "skill_name": "scenario-orchestrator",
  "display_name": "场景业务编排",
  "description": "说明什么整体场景请求应触发总控，以及如何路由",
  "invocation_triggers": ["至少两个具体触发条件"],
  "foundation_ids": [],
  "main_flow": [],
  "routing": [],
  "failure_policy": [
    "输入或规则不足时停止对应阶段并报告缺口",
    "待确认分支不得根据历史频次自动选择"
  ],
  "non_goals": ["不替代阶段 Skill 执行业务责任"]
}
~~~

只填写 `skill_name`、`description`、`invocation_triggers`、`failure_policy` 和 `non_goals`。主流程与路由不得改写。

## 命名

- `bundle.name` 和所有 `skill_name` 使用小写 kebab-case；
- 长度不超过 63；
- 名称包含场景或领域含义，避免 `step-1`、`process-data`、`generic-reader`；
- 所有 Skill 名称全局唯一。

## 不支持格式

模板会为无法识别的扩展名生成 `unsupported_formats` 项。必须填写原因，例如“当前没有随包提供二进制格式解析器，需要第三方先转换为 PDF”。不得删除或伪装成已支持。
