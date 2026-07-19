# 宏观数据关系综合协议

## 目录

1. [目标与粒度](#目标与粒度)
2. [节点与关系](#节点与关系)
3. [综合方法](#综合方法)
4. [候选 JSON](#候选-json)
5. [交付前检查](#交付前检查)

## 目标与粒度

最终产物是一张可供后续业务流程推导使用的“场景数据底图”，不是字段 ER 图，也不是完整业务流程图。

合格节点是跨记录稳定成立的业务概念，例如：

- 就诊上下文数据
- 医疗收费明细
- 医保结算数据
- 审计规则
- 规则驱动判定
- 违规审计结果

禁止把以下内容作为节点：

- 文件名、工作表名或字段名；
- 某个 ID、编号、代码、金额、日期、姓名；
- “字段 A 等于字段 B”；
- 只在一条样本记录中成立的描述。

一般使用 5-8 个节点。超过 10 个节点或 14 条边会被校验器拒绝。若达到上限，应先合并为业务数据域，而不是提高上限。

## 节点与关系

节点类型：

trigger、actor、input、activity、object、rule、decision、state、system、output。

宏观数据关系优先使用：

| 类型 | 方向 | 含义 |
|---|---|---|
| feeds | 数据/对象 → 处理、判定或结果 | 该数据域向下游提供判断所需信息 |
| joins_with | 数据/对象 → 数据/对象 | 两个数据域存在可核验的业务关联键或追溯关系 |
| governs | 规则 → 处理、判定或结果 | 规则约束判定或结果生成 |
| derives | 数据/处理/判定 → 对象或输出 | 下游对象或结果由上游形成 |
| references | 任意概念 → 被引用概念 | 有明确引用但不表示数据流 |
| depends_on | 任意概念 → 依赖概念 | 有稳定依赖但方向/处理方式不宜进一步断言 |

triggers、precedes、branches_to、returns_to 只用于材料明确表达的流程时序或条件分支。不要仅凭字段重合使用这些类型。

旧方向类型若使用，必须遵守：

- activity/decision --consumes--> input/object
- activity/decision --produces--> output/object/state
- activity/decision --governed_by--> rule
- activity/decision --performed_by--> actor

## 综合方法

1. 先给每份材料分配角色：业务数据、规则、辅助知识、结果样例或说明。
2. 将同一含义的材料合并为一个业务数据域；文件名保留在证据来源中。
3. 用 field_relationship 证明跨材料可关联，只生成一条 joins_with 宏观边，不展开字段对。
4. 用场景描述和关系句识别哪些数据进入判定、哪些规则约束判定、结果如何形成。
5. 选择一条“核心业务数据 → 判定/加工 → 最终结果”的主数据路径，写入兼容字段 main_chain。
6. 将其他数据域、规则和辅助知识用侧向边接入；没有真实条件分叉时 branches 应为空。
7. 每个节点和边引用最小充分证据，不要把所有证据卡重复挂到每条关系。
8. 核对每个纳入文件确实被某个节点或边使用；不相关文件需有证据地排除。

主数据路径不是“表 A → 表 B → 表 C”，也不是为了连通而编造的“输入 → 匹配 → 判断”。它表达的是后续推导真正需要的数据依赖骨架。

### 方向示例

对于审计场景，可形成：

就诊上下文数据 --feeds--> 规则驱动判定

医疗收费明细 --feeds--> 规则驱动判定

审计规则 --governs--> 规则驱动判定

规则驱动判定 --derives--> 违规审计结果

就诊上下文数据 --joins_with--> 医疗收费明细

这个示例只说明粒度和方向，不能代替当前场景的证据。

## 候选 JSON

~~~json
{
  "schema_version": 1,
  "scenario": {
    "name": "场景名称",
    "purpose": "宏观数据依赖范围"
  },
  "nodes": [
    {
      "id": "n_business_data",
      "name": "业务数据域",
      "type": "input",
      "description": "跨记录稳定的数据概念",
      "evidence_ids": ["E-..."]
    }
  ],
  "edges": [
    {
      "id": "e_data_to_decision",
      "source": "n_business_data",
      "target": "n_decision",
      "type": "feeds",
      "label": "提供判定所需业务信息",
      "confidence": 0.94,
      "evidence_ids": ["E-..."]
    }
  ],
  "main_chain": ["n_business_data", "n_decision", "n_result"],
  "branches": [],
  "coverage": {
    "included_files": ["input.xlsx", "rules.xlsx"],
    "excluded_files": []
  }
}
~~~

ID 使用简短稳定的 ASCII 标识。main_chain 至少三个节点，相邻节点必须有同方向的 feeds、derives 或其他合法数据流边。

## 交付前检查

- 最终节点是否都是业务概念，而不是文件、表或字段？
- 图是否保持在 5-8 个节点附近，并且每条边都服务于后续业务推导？
- 主数据路径是否从业务数据贯穿到最终结果？
- 规则是否以 governs 等侧向关系接入，而不是伪装成流程步骤？
- field_relationship 是否只形成宏观追溯关系，没有展开成密集字段网络？
- 是否把“可能”误写成了事实？
- 每个证据 ID 是否直接支撑其节点或关系？
- 三个顶层交付文件是否存在，且没有把 _field-evidence/ 当成果？
