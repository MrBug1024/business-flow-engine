"""Helpers for deriving scenario validation state from assistant responses."""

from __future__ import annotations

_PASS_PHRASES = (
    "验证通过",
    "验证已通过",
    "验证成功",
    "校验通过",
    "测试通过",
    "可以发布",
    "满足验证要求",
)

_BLOCK_PHRASES = (
    "验证未通过",
    "未通过",
    "不通过",
    "验证失败",
    "测试失败",
    "不能发布",
    "尚未",
    "未完成",
    "未满足",
    "需要修正",
    "无法确认",
    "请先",
    "不是",
)


def response_marks_verified(content: str) -> bool:
    """Return True only for a clear positive validation conclusion."""
    text = (content or "").strip()
    if not text:
        return False
    tail = text[-1200:]
    if any(phrase in tail for phrase in _BLOCK_PHRASES):
        return False
    return any(phrase in tail for phrase in _PASS_PHRASES)
