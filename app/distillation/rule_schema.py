"""规则表结构映射推断（v1.0.3 接口，委托给 v1.0.4 的 knowledge_schema 模块）。

v1.0.4 变更说明：
* 本模块保留向后兼容接口，新代码请直接使用 knowledge_schema 模块。
* `infer_rule_schema` → 委托给 `knowledge_schema.infer_knowledge_schema`，
   返回的 RuleSchemaMapping 内部通过 to_knowledge_schema() 可转换为新结构。
* 术语对照：
    rule_table          → knowledge_table
    discriminator_column → dispatch_key_column
    discriminator_to_template → dispatch_map
    nl_description_columns → condition_columns
    business_field_map  → field_role_map
"""

from __future__ import annotations

from app.domain.models import RuleSchemaMapping, Scenario, TableMeta
from . import knowledge_schema as _ks


def find_rule_table(scenario: Scenario) -> TableMeta | None:
    """向后兼容：定位规则/知识表。"""
    return _ks.find_knowledge_table(scenario)


def infer_rule_schema(scenario: Scenario) -> RuleSchemaMapping | None:
    """向后兼容：从规则/知识表蒸馏结构映射。

    内部委托给 knowledge_schema.infer_knowledge_schema，
    将结果转换回 RuleSchemaMapping 以保持旧接口兼容性。
    """
    ks = _ks.infer_knowledge_schema(scenario)
    if ks is None:
        return None

    # 将 KnowledgeSchemaMapping 转回 RuleSchemaMapping（兼容旧调用方）
    return RuleSchemaMapping(
        rule_table=ks.knowledge_table,
        discriminator_column=ks.dispatch_key_column,
        rule_id_column=ks.item_id_column,
        nl_description_columns=ks.condition_columns,
        parameter_columns=ks.parameter_columns,
        discriminator_to_template=ks.dispatch_map,
        business_field_map=ks.field_role_map,
        summary=ks.summary,
    )
