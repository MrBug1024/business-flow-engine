"""OpenAI-compatible streaming gateway for the Studio Agent runtime."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.config import settings as env_settings
from app.studio.models import BusinessRecord
from app.studio.capabilities.registry import list_skills
from app.studio.settings import studio_settings
from app.studio.capabilities.tools import tool_registry


StreamKind = Literal["content", "reasoning", "tool_call_delta", "completed"]


class ModelGatewayError(RuntimeError):
    """A provider request failed before a valid model turn completed."""


@dataclass(slots=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ModelToolCallChunk:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(slots=True)
class ModelStreamEvent:
    kind: StreamKind
    content: str = ""
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    tool_call_chunks: list[ModelToolCallChunk] = field(default_factory=list)


def stream_model_turn(
    record: BusinessRecord,
    messages: list[dict[str, Any]],
    requested_model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> Iterator[ModelStreamEvent]:
    """Stream one model turn, including reasoning and function calls."""

    _enforce_request_budgets(messages, tools)
    model_config = studio_settings.active_model_config(
        requested_model,
        owner_id=record.owner_id,
    )
    api_key = model_config.api_key.strip() or env_settings.openai_api_key.strip()
    if not api_key or api_key.casefold() in {"your-api-key-here", "sk-xxx", "changeme"}:
        yield ModelStreamEvent(kind="content", content=_model_unavailable_message(record, "没有配置可用的 API Key"))
        yield ModelStreamEvent(kind="completed")
        return
    base_url = model_config.base_url.strip() or env_settings.openai_base_url.strip()
    if not base_url:
        yield ModelStreamEvent(kind="content", content=_model_unavailable_message(record, "当前模型没有配置 base_url"))
        yield ModelStreamEvent(kind="completed")
        return

    payload: dict[str, Any] = {
        "model": model_config.model,
        "messages": messages,
        "temperature": env_settings.llm_temperature,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
        payload["parallel_tool_calls"] = env_settings.llm_parallel_tool_calls

    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    markup = ThinkingMarkupParser()
    pending_calls: dict[int, dict[str, str]] = {}

    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - administrator-configured endpoint
            yielded = False
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = _extract_delta(chunk)
                reasoning = _text_value(delta.get("reasoning_content") or delta.get("reasoning"))
                if reasoning:
                    yielded = True
                    yield ModelStreamEvent(kind="reasoning", content=reasoning)
                content = _text_value(delta.get("content"))
                if content:
                    yielded = True
                    for part_kind, part in markup.feed(content):
                        yield ModelStreamEvent(kind=part_kind, content=part)
                raw_tool_calls = delta.get("tool_calls") or []
                tool_call_chunks = _tool_call_chunks(raw_tool_calls)
                if tool_call_chunks:
                    yielded = True
                    yield ModelStreamEvent(
                        kind="tool_call_delta",
                        tool_call_chunks=tool_call_chunks,
                    )
                _merge_tool_call_deltas(pending_calls, raw_tool_calls)

            for part_kind, part in markup.flush():
                yielded = True
                yield ModelStreamEvent(kind=part_kind, content=part)

            calls = _finalize_tool_calls(pending_calls)
            if calls:
                yielded = True
            if not yielded:
                yield ModelStreamEvent(kind="content", content=_model_unavailable_message(record, "模型没有返回可读内容"))
            yield ModelStreamEvent(kind="completed", tool_calls=calls)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelGatewayError(f"模型连接失败：{exc}") from exc


def strip_thinking_markup(text: str) -> str:
    text = re.sub(r"(?is)<think>.*?</think>", "", text)
    text = re.sub(r"(?is)</?think>", "", text)
    return text.strip()


def request_context_size(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> dict[str, int]:
    """Measure high-risk request sections without logging their contents."""

    system_chars = sum(
        len(_text_value(message.get("content")))
        for message in messages
        if str(message.get("role") or "").casefold() == "system"
    )
    tool_schema_chars = len(
        json.dumps(tools or [], ensure_ascii=False, separators=(",", ":"), default=str)
    )
    return {
        "system_chars": system_chars,
        "tool_schema_chars": tool_schema_chars,
    }


def _enforce_request_budgets(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> None:
    sizes = request_context_size(messages, tools)
    system_limit = max(1_000, int(env_settings.agent_system_prompt_character_limit))
    tool_limit = max(5_000, int(env_settings.agent_tool_schema_character_limit))
    if sizes["system_chars"] > system_limit:
        raise ModelGatewayError(
            "模型请求已阻止：System 提示词异常膨胀 "
            f"({sizes['system_chars']}/{system_limit} 字符)。"
        )
    if sizes["tool_schema_chars"] > tool_limit:
        raise ModelGatewayError(
            "模型请求已阻止：默认挂载的工具定义异常膨胀 "
            f"({sizes['tool_schema_chars']}/{tool_limit} 字符)，请改为按需发现能力。"
        )


def _extract_delta(chunk: dict[str, Any]) -> dict[str, Any]:
    choices = chunk.get("choices") or []
    if not choices:
        return {}
    first = choices[0]
    delta = first.get("delta") or first.get("message") or {}
    if isinstance(delta, dict):
        return delta
    text = first.get("text")
    return {"content": text} if text else {}


def _text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("text"):
            parts.append(str(item["text"]))
    return "".join(parts)


def _merge_tool_call_deltas(target: dict[int, dict[str, str]], deltas: list[Any]) -> None:
    for fallback_index, raw in enumerate(deltas):
        if not isinstance(raw, dict):
            continue
        index = int(raw.get("index", fallback_index))
        current = target.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if raw.get("id"):
            current["id"] = str(raw["id"])
        function = raw.get("function") or {}
        if isinstance(function, dict):
            if function.get("name"):
                current["name"] += str(function["name"])
            if function.get("arguments"):
                current["arguments"] += str(function["arguments"])


def _tool_call_chunks(deltas: list[Any]) -> list[ModelToolCallChunk]:
    chunks: list[ModelToolCallChunk] = []
    for fallback_index, raw in enumerate(deltas):
        if not isinstance(raw, dict):
            continue
        try:
            index = int(raw.get("index", fallback_index))
        except (TypeError, ValueError):
            index = fallback_index
        function = raw.get("function") or {}
        if not isinstance(function, dict):
            function = {}
        chunk = ModelToolCallChunk(
            index=index,
            id=str(raw.get("id") or ""),
            name=str(function.get("name") or ""),
            arguments=str(function.get("arguments") or ""),
        )
        if chunk.id or chunk.name or chunk.arguments:
            chunks.append(chunk)
    return chunks


def _finalize_tool_calls(pending: dict[int, dict[str, str]]) -> list[ModelToolCall]:
    calls: list[ModelToolCall] = []
    for index, raw in sorted(pending.items()):
        if not raw["name"]:
            continue
        try:
            arguments = json.loads(raw["arguments"] or "{}")
        except json.JSONDecodeError:
            arguments = {"_raw": raw["arguments"]}
        if not isinstance(arguments, dict):
            arguments = {"value": arguments}
        calls.append(
            ModelToolCall(
                id=raw["id"] or f"tool_call_{index}",
                name=raw["name"],
                arguments=arguments,
            )
        )
    return calls


class ThinkingMarkupParser:
    """Split providers that encode reasoning inside ``<think>`` tags."""

    def __init__(self) -> None:
        self.buffer = ""
        self.in_thinking_block = False

    def feed(self, chunk: str) -> Iterator[tuple[Literal["content", "reasoning"], str]]:
        self.buffer += chunk
        while self.buffer:
            if self.in_thinking_block:
                end = self.buffer.lower().find("</think>")
                if end < 0:
                    if len(self.buffer) > 16:
                        reasoning = self.buffer[:-16]
                        self.buffer = self.buffer[-16:]
                        if reasoning:
                            yield "reasoning", reasoning
                    return
                reasoning = self.buffer[:end]
                self.buffer = self.buffer[end + len("</think>") :]
                self.in_thinking_block = False
                if reasoning:
                    yield "reasoning", reasoning
                continue

            start = self.buffer.lower().find("<think>")
            if start < 0:
                if len(self.buffer) <= 16:
                    return
                visible = self.buffer[:-16]
                self.buffer = self.buffer[-16:]
                if visible:
                    yield "content", visible
                return

            visible = self.buffer[:start]
            self.buffer = self.buffer[start + len("<think>") :]
            self.in_thinking_block = True
            if visible:
                yield "content", visible

    def flush(self) -> Iterator[tuple[Literal["content", "reasoning"], str]]:
        if self.buffer:
            kind: Literal["content", "reasoning"] = "reasoning" if self.in_thinking_block else "content"
            yield kind, self.buffer
        self.buffer = ""


def _model_unavailable_message(record: BusinessRecord, reason: str) -> str:
    skill_names = ", ".join(
        skill.name for skill in list_skills(record.owner_id)
    ) or "暂无"
    tool_names = ", ".join(tool.name for tool in tool_registry.list() if tool.mounted) or "暂无"
    return (
        "我现在没有拿到真实模型的可用回复，所以不会假装已经完成业务分析。\n\n"
        f"原因：{reason}\n\n"
        f"当前真实 Skill：{skill_names}\n"
        f"当前平台 Tool：{tool_names}\n\n"
        "配置可用模型后，Agent 会自主读取工作区，并按需调用 Tool、Skill 与 MCP。"
    )
