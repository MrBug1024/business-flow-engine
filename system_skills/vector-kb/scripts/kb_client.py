#!/usr/bin/env python3
"""
向量知识库客户端。默认连接配置随 Skill 打包，场景可显式覆盖。

用法:
    python scripts/kb_client.py "<问题>" [limit]

示例:
    python scripts/kb_client.py "验收服务器的部署流程" 5

标准输出: 统一的 status/data/sources JSON 结果（供程序解析）
标准错误: 人类可读的格式化结果
"""

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


_SKILL_ROOT = Path(__file__).resolve().parents[1]
_DEFAULTS_FILE = _SKILL_ROOT / "config" / "defaults.json"
_SCENARIO_BINDING_FILE = _SKILL_ROOT / "references" / "scenario_binding.json"


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _configuration() -> tuple[dict[str, str], list[str]]:
    defaults = _read_object(_DEFAULTS_FILE)
    binding = _read_object(_SCENARIO_BINDING_FILE)
    overrides = binding.get("runtime_overrides") or {}
    if not isinstance(overrides, dict):
        overrides = {}
    config: dict[str, str] = {}
    environment_keys = {
        "base_url": "VECTOR_KB_BASE_URL",
        "library_id": "VECTOR_KB_LIBRARY_ID",
        "api_key": "VECTOR_KB_API_KEY",
        "timeout_seconds": "VECTOR_KB_TIMEOUT_SECONDS",
    }
    for target, environment_key in environment_keys.items():
        value: Any = os.environ.get(
            environment_key,
            overrides.get(target, defaults.get(target, "")),
        )
        config[target] = str(value or "").strip()
    config["base_url"] = config["base_url"].rstrip("/")
    missing = [key for key in ("base_url", "library_id", "api_key") if not config[key]]
    return config, missing


def _result(status: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "data": [],
        "sources": [],
        **extra,
    }


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _valid_base_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def search_kb(question: str, limit: int = 5) -> dict[str, Any]:
    """检索知识库并返回稳定的 Agent 结果契约。"""
    config, missing = _configuration()
    if missing:
        return _result(
            "configuration_required",
            "知识库 Skill 缺少默认配置或场景覆盖：" + ", ".join(missing),
            missing=missing,
        )
    query = str(question or "").strip()
    if not query:
        return _result("error", "检索问题不能为空。")
    if not _valid_base_url(config["base_url"]):
        return _result("error", "知识库 base_url 必须是无内嵌凭据的 HTTP/HTTPS 地址。")
    try:
        requested_limit = max(1, min(int(limit), 50))
        timeout = max(1, min(int(config["timeout_seconds"] or 30), 300))
    except (TypeError, ValueError):
        return _result("error", "limit 或 timeout_seconds 配置无效。")
    try:
        response = requests.post(
            f"{config['base_url']}/libraries/{config['library_id']}/query",
            headers=_headers(config["api_key"]),
            json={"query": query, "limit": requested_limit},
            timeout=timeout,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return _result("provider_unavailable", "知识库服务当前不可达，请稍后重试。")
    except requests.exceptions.RequestException:
        return _result("provider_unavailable", "知识库请求未能完成，请检查服务状态。")

    if response.status_code in {401, 403}:
        return _result("auth_failed", "知识库访问凭据无效或权限不足。")
    if response.status_code == 429 or response.status_code >= 500:
        return _result("provider_unavailable", "知识库服务暂时不可用，请稍后重试。")
    if response.status_code >= 400:
        return _result("error", f"知识库请求失败（HTTP {response.status_code}）。")
    try:
        payload = response.json()
    except (requests.exceptions.JSONDecodeError, ValueError):
        return _result("error", "知识库返回了无法解析的响应。")
    raw_results = payload.get("results", []) if isinstance(payload, dict) else []
    chunks = [item for item in raw_results if isinstance(item, dict)] if isinstance(raw_results, list) else []
    sources = [
        {
            "title": item.get("title") or "未知标题",
            "document_id": item.get("document_id") or "",
            "chunk_id": item.get("chunk_id") or "",
            "similarity": item.get("similarity"),
        }
        for item in chunks
    ]
    if not chunks:
        return _result("no_results", "未检索到相关知识库内容。")
    return _result(
        "success",
        f"已检索到 {len(chunks)} 条相关内容。",
        data=chunks,
        sources=sources,
    )


def get_chunk_source(document_id: str, chunk_id: str | None = None) -> dict[str, Any]:
    """查看切片原文；配置与错误仍使用统一结果契约。"""
    config, missing = _configuration()
    if missing:
        return _result(
            "configuration_required",
            "知识库 Skill 缺少默认配置或场景覆盖：" + ", ".join(missing),
            missing=missing,
        )
    if not _valid_base_url(config["base_url"]):
        return _result("error", "知识库 base_url 配置无效。")
    if not str(document_id or "").strip():
        return _result("error", "document_id 不能为空。")
    try:
        timeout = max(1, min(int(config["timeout_seconds"] or 30), 300))
    except (TypeError, ValueError):
        return _result("error", "timeout_seconds 配置无效。")
    try:
        response = requests.get(
            f"{config['base_url']}/libraries/{config['library_id']}/documents/{document_id}/source",
            headers=_headers(config["api_key"]),
            params={"chunk_id": chunk_id} if chunk_id else None,
            timeout=timeout,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return _result("provider_unavailable", "知识库服务当前不可达，请稍后重试。")
    except requests.exceptions.RequestException:
        return _result("provider_unavailable", "知识库请求未能完成，请检查服务状态。")
    if response.status_code in {401, 403}:
        return _result("auth_failed", "知识库访问凭据无效或权限不足。")
    if response.status_code == 429 or response.status_code >= 500:
        return _result("provider_unavailable", "知识库服务暂时不可用，请稍后重试。")
    if response.status_code >= 400:
        return _result("error", f"知识库原文请求失败（HTTP {response.status_code}）。")
    try:
        data = response.json()
    except (requests.exceptions.JSONDecodeError, ValueError):
        return _result("error", "知识库返回了无法解析的原文响应。")
    return _result(
        "success",
        "已读取知识库原文位置。",
        data=data,
        sources=[{"document_id": document_id, "chunk_id": chunk_id or ""}],
    )


def build_context(chunks: list[dict[str, Any]]) -> str:
    """将切片列表拼装为 LLM prompt 用的上下文字符串。"""
    parts = []
    for i, c in enumerate(chunks, 1):
        parts.append(
            f"[来源 {i}]\n"
            f"标题：{c.get('title') or '未知'}\n"
            f"相关性：{c.get('similarity')}\n"
            f"document_id：{c.get('document_id')}\n"
            f"chunk_id：{c.get('chunk_id')}\n"
            f"正文：\n{c.get('text') or ''}"
        )
    return "\n\n---\n\n".join(parts)


def _pretty(chunks: list[dict[str, Any]]) -> str:
    """格式化为人类可读文本（输出到 stderr）。"""
    if not chunks:
        return "未检索到相关内容，建议更换关键词重试。"
    lines = [f"共检索到 {len(chunks)} 条切片：\n"]
    for i, c in enumerate(chunks, 1):
        lines += [
            f"[{i}] {c.get('title') or '未知标题'}",
            f"    相关性：{c.get('similarity')}",
            f"    document_id：{c.get('document_id')}",
            f"    chunk_id：{c.get('chunk_id')}",
            f"    正文：{str(c.get('text') or '')[:200]}...",
            "",
        ]
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scripts/kb_client.py \"<问题>\" [limit]", file=sys.stderr)
        sys.exit(1)

    question = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    result = search_kb(question, limit=limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] in {"success", "no_results"}:
        print(_pretty(result["data"]), file=sys.stderr)
        sys.exit(0)
    sys.exit(2 if result["status"] == "configuration_required" else 1)
