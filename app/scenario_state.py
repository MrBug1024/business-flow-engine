"""场景推导产物的失效规则。

上游数据一变，下游产物必须清空或重推；否则前端会同时显示新表结构和旧流程/旧技能，
让用户感觉步骤之间脱节。
"""

from __future__ import annotations

from .models import Scenario, ScenarioStatus


def invalidate_from_tables(scenario: Scenario) -> None:
    """表文件或表角色变化后，清空所有依赖表结构的推导产物。"""
    scenario.trace_chain = {}
    scenario.relations = None
    scenario.flow = None
    scenario.domain_knowledge = None
    scenario.outputs = []
    scenario.validations = []
    scenario.skills = []
    if scenario.status != ScenarioStatus.CREATED:
        scenario.status = ScenarioStatus.TABLES_UPLOADED


def invalidate_after_trace(scenario: Scenario) -> None:
    """链路追踪变化后，清空关联及其后的产物。"""
    scenario.relations = None
    scenario.flow = None
    scenario.domain_knowledge = None
    scenario.outputs = []
    scenario.validations = []
    scenario.skills = []
    scenario.status = ScenarioStatus.TRACE_SAMPLED


def invalidate_after_relations(scenario: Scenario) -> None:
    """关联关系变化后，清空流程及其后的产物。"""
    scenario.flow = None
    scenario.domain_knowledge = None
    scenario.outputs = []
    scenario.validations = []
    scenario.skills = []
    scenario.status = ScenarioStatus.RELATIONS_DEDUCED


def invalidate_after_flow(scenario: Scenario) -> None:
    """流程变化后，清空技能和验证产物。"""
    scenario.domain_knowledge = None
    scenario.skills = []
    scenario.validations = []
    scenario.status = ScenarioStatus.FLOW_DEDUCED
