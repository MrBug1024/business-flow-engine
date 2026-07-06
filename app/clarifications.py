"""待确认事项整理：去重、降噪，并生成普通用户可选择的答案。

这里不判断具体业务结论，只负责把推导阶段产生的疑问整理成更好的交互形态：
- 重复问题只保留一次；
- 低置信、纯字段名猜测的关联问题不打扰用户；
- 必须问的问题提供带推荐项的可选答案。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .models import Clarification


_LOW_CONF_RELATION_THRESHOLD = 0.8
_BULK_RELATION_SEPARATORS = ("、", "，", ",", "；", ";")


@dataclass(frozen=True)
class _RelationQuestion:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    confidence: float | None = None


_CONF_RE = re.compile(r"(?:置信度|confidence)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*%?", re.I)
_REL_RE = re.compile(
    r"(?P<ft>[^.\s【】]+)\.(?P<fc>[^与和\-→]+?)\s*"
    r"(?:与|和|->|→)\s*"
    r"(?P<tt>[^.\s【】]+)\.(?P<tc>.+?)"
    r"(?=\s*(?:是否|是不是|确为|确认为|能否|可否|是)|[？?，,；;（）()]|$)"
)


def build_clarifications(
    raw_questions: Iterable[str],
    *,
    context: str = "general",
    max_items: int = 8,
) -> list[Clarification]:
    """把任意来源的待确认字符串转换成结构化问题。

    `context` 只用于生成稳定 id，不携带业务含义。
    """
    clarifications: list[Clarification] = []
    seen: set[str] = set()

    for raw in raw_questions:
        question = str(raw or "").strip()
        if not question:
            continue
        key, clarification = _to_clarification(question)
        if clarification is None or key in seen:
            continue
        seen.add(key)
        clarifications.append(clarification)
        if len(clarifications) >= max_items:
            break

    for i, item in enumerate(clarifications, start=1):
        item.id = item.id or f"{context}_q{i}"
    return clarifications


def normalized_question_texts(clarifications: Iterable[Clarification]) -> list[str]:
    """用于兼容旧的 `ambiguous_questions` 文本输出。"""
    return [c.question for c in clarifications]


def _to_clarification(question: str) -> tuple[str, Clarification | None]:
    if _is_noise(question):
        return _normal_key(question), None

    rel = _parse_relation_question(question)
    if rel:
        key = _relation_key(rel)
        if rel.confidence is not None and rel.confidence < _LOW_CONF_RELATION_THRESHOLD:
            return key, None
        return key, _relation_clarification(rel)

    if _looks_like_bulk_relation_probe(question):
        return _normal_key(question), None

    if "复合键" in question or "组合" in question and "匹配" in question:
        return _normal_key(question), Clarification(
            question=_plain_composite_question(question),
            options=[
                "使用多个字段一起定位（推荐）",
                "先保留当前单字段关联",
                "这张表暂不参与流程",
                "其他（我来说明）",
            ],
            allow_custom=True,
        )

    if "重新推导" in question or "降级" in question:
        return _normal_key(question), Clarification(
            question="本次流程是降级生成的骨架，可能没有完整还原真实业务流程。接下来怎么处理？",
            options=[
                "重新推导流程（推荐）",
                "先保留当前流程骨架",
                "我会手工补充流程",
                "其他（我来说明）",
            ],
            allow_custom=True,
        )

    if "硬编码" in question or "按行读取" in question or "具体记录" in question:
        return _normal_key(question), Clarification(
            question="流程节点里出现了一个来自样本数据的固定值。这个值应该如何处理？",
            options=[
                "改成从数据/知识表逐行读取（推荐）",
                "它是本场景固定不变的口径",
                "先删除这个固定值",
                "其他（我来说明）",
            ],
            allow_custom=True,
        )

    if "追踪链路" in question or ("追踪" in question and "表" in question):
        return _normal_key(question), Clarification(
            question=_plain_generic_question(question),
            options=[
                "这些表参与流程，我来补充关联键（推荐）",
                "这些表不参与本次流程",
                "先按当前样本继续",
                "其他（我来说明）",
            ],
            allow_custom=True,
        )

    if "分派" in question or "知识表" in question:
        return _normal_key(question), Clarification(
            question=_plain_generic_question(question),
            options=[
                "采用系统当前识别结果（推荐）",
                "没有这类结构，先跳过",
                "不确定，先暂停",
                "其他（我来说明）",
            ],
            allow_custom=True,
        )

    return _normal_key(question), Clarification(
        question=_plain_generic_question(question),
        options=[
            "按系统当前建议继续（推荐）",
            "不是，忽略这一项",
            "不确定，先跳过",
            "其他（我来说明）",
        ],
        allow_custom=True,
    )


def _parse_relation_question(question: str) -> _RelationQuestion | None:
    if "关联" not in question and "键" not in question:
        return None
    match = _REL_RE.search(question)
    if not match:
        return None
    conf_match = _CONF_RE.search(question)
    confidence: float | None = None
    if conf_match:
        raw = float(conf_match.group(1))
        confidence = raw / 100 if raw > 1 else raw
    return _RelationQuestion(
        from_table=match.group("ft").strip(),
        from_column=match.group("fc").strip(),
        to_table=match.group("tt").strip(),
        to_column=match.group("tc").strip(),
        confidence=confidence,
    )


def _relation_clarification(rel: _RelationQuestion) -> Clarification:
    left = f"{rel.from_table}.{rel.from_column}"
    right = f"{rel.to_table}.{rel.to_column}"
    conf = f"系统证据约 {rel.confidence:.0%}。" if rel.confidence is not None else ""
    if rel.confidence is not None and rel.confidence < 0.9:
        options = [
            "先跳过，不作为关联（推荐）",
            "是，它们是同一个业务键",
            "不是，不要用它们关联",
            "其他（我来说明）",
        ]
    else:
        options = [
            "是，它们是同一个业务键（推荐）",
            "不是，不要用它们关联",
            "不确定，先跳过",
            "其他（我来说明）",
        ]
    return Clarification(
        question=(
            f"系统发现「{left}」和「{right}」可能用来连接两张表。"
            f"它们在业务上是否表示同一个对象、单据或编号？{conf}"
        ),
        options=options,
        allow_custom=True,
    )


def _relation_key(rel: _RelationQuestion) -> str:
    endpoints = sorted([
        f"{rel.from_table}.{rel.from_column}",
        f"{rel.to_table}.{rel.to_column}",
    ])
    return "relation:" + "|".join(endpoints)


def _normal_key(question: str) -> str:
    q = _CONF_RE.sub("", question)
    q = re.sub(r"\s+", "", q)
    q = re.sub(r"[：:？?。，,；;！!（）()\[\]【】「」\"'`]+", "", q)
    return q[:180]


def _is_noise(question: str) -> bool:
    return (
        question.startswith("❌")
        or "已剔除一条无效关联" in question
        or "不是本场景中存在的表" in question
        or "这一列在真实表结构里不存在" in question
    )


def _looks_like_bulk_relation_probe(question: str) -> bool:
    if "是否" not in question or "关联" not in question:
        return False
    sep_count = sum(question.count(sep) for sep in _BULK_RELATION_SEPARATORS)
    return sep_count >= 2


def _plain_composite_question(question: str) -> str:
    match = re.search(r"【(.+?)】", question)
    table = f"「{match.group(1)}」" if match else "这张表"
    return (
        f"{table} 只靠一个字段可能会匹配到多行。是否使用多个字段一起定位同一条业务记录？"
    )


def _plain_generic_question(question: str) -> str:
    cleaned = question.strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:180]
