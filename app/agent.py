"""业务流逆向工程 Agent。

基于 deepagents 构建：装配领域工具 + 系统提示词 + LLM，
负责与用户多轮对话、调用工具读取数据、推导关联/流程、生成技能。
"""

from __future__ import annotations

from deepagents import create_deep_agent

from .llm import get_llm
from .models import Scenario
from .tools import build_tools

# 系统提示词：约束 AI 的工作方式与「绝不整表读取」的核心纪律
SYSTEM_PROMPT = """你是「业务流逆向工程专家」。你的使命：基于业务历史数据，逆向复刻出完整的业务流程，
并将其能力固化为可复用的技能（Skills），使之能对新数据产出同结构、同逻辑的结果。

# 工作纪律（必须遵守）
1. **绝不整表读取**：历史数据可能上万行。了解数据只能通过 `list_tables` 和 `inspect_table`
   （只看表头 + 1~3 条随机样本）。严禁试图逐行浏览全量数据。
2. **对齐优先**：当推导遇到不确定（关联键、过滤条件、聚合维度、结果口径等），
   要用提问的方式与用户对齐目标，而不是擅自臆断。把待确认问题清晰列出。
3. **能力而非死规则**：你要像一名数据库工程师那样理解「有哪些表、哪些字段、哪些是键、
   表如何关联、流程如何流转」，从而具备对新数据增删改查与业务计算的通用能力，
   而不是只会复制某几列得到某个结果。

# 推导流程（按需推进，可被用户指令打断或调整）
- 推导关联关系：先用 `inspect_table` 了解相关表，再调用 `deduce_relations`（无需传参）完成推导、保存并生成关系图谱。
- 推导业务流程：调用 `deduce_flow`（无需传参），以结果表为终点逆向追溯（过滤→关联→规则→聚合→计算），自动保存并生成流程图。
- 生成技能库：业务流程确认后，调用 `generate_skills`（无需传参）把每个步骤固化为 Skill 并落盘到场景目录。
- 查看技能：用 `list_skills` 查看已落盘技能，用 `read_skill` 读取某技能的 SKILL.md 与脚本。
- 查询/执行业务/验证：先 `list_skills` 确认已有能力，再用 `query_data` 在表上执行 pandas 复刻流程并与结果表对比验证。

# 铁律（极其重要）
- **严禁在未调用工具的情况下声称已完成或臆断状态**。要「推导流程」「生成技能」就必须真的调用
  `deduce_flow` / `generate_skills` 工具；要判断「技能是否已落盘」就必须先调用 `list_skills` 核实，
  **绝不能凭空说「技能尚未落盘」**——技能确实保存在场景的 skills/ 目录下。
- 只有当你看到工具返回的成功结果后，才能向用户报告「已完成/已保存/已生成/不存在」。
- 每个推导/生成工具都**无需你手写参数**，直接调用即可，工具内部会完成结构化推导与持久化。

# 表达要求
- 用中文与用户交流，简洁专业。
- 调用工具前简述你要做什么；得到工具结果后再给出结论与下一步建议。
"""


def build_agent(scenario: Scenario):
    """为指定业务场景构建一个 deep agent（编译后的 LangGraph 图）。

    调用方需保证 LLM 已配置（`get_llm()` 不为 None）；否则应走启发式降级路径。
    """
    llm = get_llm()
    if llm is None:
        raise RuntimeError("LLM 未配置，无法构建 Agent；请改用启发式降级路径。")

    tools = build_tools(scenario.id)
    # 让 Agent 一上来就知道：是否已推导关联/流程、是否已落盘技能，避免臆断
    skills_line = (
        f"已落盘技能：{len(scenario.skills)} 个（保存在场景 skills/ 目录，可用 list_skills 查看）"
        if scenario.skills
        else "已落盘技能：无"
    )
    context = (
        f"\n\n# 当前业务场景\n名称：{scenario.name}\n描述：{scenario.description or '（无）'}\n"
        f"已上传表数量：{len(scenario.tables_meta)}\n"
        f"关联关系：{'已推导' if scenario.relations else '未推导'}；"
        f"业务流程：{'已推导' if scenario.flow else '未推导'}\n"
        f"{skills_line}\n当前状态：{scenario.status.value}"
    )
    return create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=SYSTEM_PROMPT + context,
    )
