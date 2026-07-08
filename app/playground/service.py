"""Services for the independent Agent platform.

The Agent platform state is user-scoped and intentionally separate from
distillation scenarios. Distillation release helpers remain here only because
the release API uses them to show third-party installation information after a
scenario has been published.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage

from app.core.agent_stream import stream_agent
from app.core.config import settings
from app.core.streaming import sse, sse_done
from app.domain.models import ChatMessage, ChatRole, ScenarioStatus, ToolTrace
from app.domain.storage import store
from app.playground import resources
from app.playground.agent import DEFAULT_SYSTEM_PROMPT, build_playground_agent
from app.release.builder import ensure_release_package, release_status
from app.runtime import scenario_runtime as rt

_ABORTED_NOTE = "（本轮执行被中断，任务已终止作废，后续消息将作为全新任务独立处理。）"


def _safe(uid: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", uid or "anon")


def _pg_dir(user_id: str) -> Path:
    d = settings.data_path / "playground" / _safe(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _conversation_id(value: str | None) -> str:
    text = _clean_id(str(value or "default")) or "default"
    return text[:80]


def _conversations_file(user_id: str) -> Path:
    return _pg_dir(user_id) / "conversations.json"


def _chats_dir(user_id: str) -> Path:
    d = _pg_dir(user_id) / "chats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _chat_file(user_id: str, conversation_id: str | None = None) -> Path:
    cid = _conversation_id(conversation_id)
    if cid == "default":
        return _pg_dir(user_id) / "chat.jsonl"
    return _chats_dir(user_id) / f"{cid}.jsonl"


def _agent_config_file(user_id: str) -> Path:
    return _pg_dir(user_id) / "agent_config.json"


def attachments_dir(user_id: str, conversation_id: str | None = None) -> Path:
    cid = _conversation_id(conversation_id)
    base = _pg_dir(user_id) / "attachments"
    d = base if cid == "default" else base / cid
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- Agent config
def _default_agent_config(_user_id: str) -> dict:
    return {
        "main_agent": {
            "name": "主 Agent",
            "system_prompt": "",
            "default_system_prompt": DEFAULT_SYSTEM_PROMPT,
            "llm_id": "",
            "sandbox_id": "",
            "enabled_skills": [],
            "enabled_mcps": [],
            "enabled_subagents": [],
        },
        "subagents": [],
    }


def _clean_id(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", value or "")[:80]


def _as_str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item)
        if text and text not in out:
            out.append(text)
    return out


# ------------------------------------------------------------- Conversations
def _read_conversation_rows(user_id: str) -> list[dict]:
    p = _conversations_file(user_id)
    try:
        rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except Exception:
        rows = []
    if not isinstance(rows, list):
        rows = []

    cleaned: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = _conversation_id(row.get("id"))
        if cid in seen:
            continue
        seen.add(cid)
        cleaned.append({
            "id": cid,
            "title": str(row.get("title") or "新对话")[:80],
            "created_at": float(row.get("created_at") or time.time()),
            "updated_at": float(row.get("updated_at") or row.get("created_at") or time.time()),
        })

    if "default" not in seen:
        legacy = _chat_file(user_id, "default")
        ts = legacy.stat().st_mtime if legacy.exists() else time.time()
        cleaned.append({
            "id": "default",
            "title": "当前对话",
            "created_at": ts,
            "updated_at": ts,
        })
    return cleaned


def _write_conversation_rows(user_id: str, rows: list[dict]) -> None:
    p = _conversations_file(user_id)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _message_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except Exception:
        return 0


def _conversation_public_row(user_id: str, row: dict) -> dict:
    cid = _conversation_id(row.get("id"))
    p = _chat_file(user_id, cid)
    updated_at = float(row.get("updated_at") or row.get("created_at") or time.time())
    if p.exists():
        updated_at = max(updated_at, p.stat().st_mtime)
    return {
        "id": cid,
        "title": str(row.get("title") or "新对话")[:80],
        "created_at": float(row.get("created_at") or updated_at),
        "updated_at": updated_at,
        "message_count": _message_count(p),
    }


def list_conversations(user_id: str) -> list[dict]:
    rows = [_conversation_public_row(user_id, row) for row in _read_conversation_rows(user_id)]
    rows.sort(key=lambda r: (r["updated_at"], r["created_at"]), reverse=True)
    return rows


def create_conversation(user_id: str, title: str = "") -> dict:
    now = time.time()
    row = {
        "id": f"conv_{uuid.uuid4().hex[:12]}",
        "title": str(title or "新对话")[:80],
        "created_at": now,
        "updated_at": now,
    }
    rows = [row, *_read_conversation_rows(user_id)]
    _write_conversation_rows(user_id, rows)
    return _conversation_public_row(user_id, row)


def delete_conversation(user_id: str, conversation_id: str) -> list[dict]:
    cid = _conversation_id(conversation_id)
    if cid == "default":
        clear_messages(user_id, cid)
        clear_attachments(user_id, cid)
        rows = _read_conversation_rows(user_id)
        for row in rows:
            if _conversation_id(row.get("id")) == "default":
                row["title"] = "当前对话"
                row["updated_at"] = time.time()
        _write_conversation_rows(user_id, rows)
        return list_conversations(user_id)

    _chat_file(user_id, cid).unlink(missing_ok=True)
    attach = attachments_dir(user_id, cid)
    if attach.exists() and attach.is_dir():
        for p in attach.iterdir():
            if p.is_file():
                p.unlink()
        try:
            attach.rmdir()
        except OSError:
            pass
    rows = [row for row in _read_conversation_rows(user_id) if _conversation_id(row.get("id")) != cid]
    _write_conversation_rows(user_id, rows)
    return list_conversations(user_id)


def _title_from_message(text: str) -> str:
    title = re.sub(r"\s+", " ", text or "").strip()
    return title[:36] or "新对话"


def _touch_conversation(user_id: str, conversation_id: str, title_hint: str = "") -> str:
    cid = _conversation_id(conversation_id)
    now = time.time()
    rows = _read_conversation_rows(user_id)
    row = next((r for r in rows if _conversation_id(r.get("id")) == cid), None)
    if not row:
        row = {"id": cid, "title": "新对话", "created_at": now, "updated_at": now}
        rows.insert(0, row)
    if title_hint and str(row.get("title") or "") in {"", "新对话", "当前对话"}:
        row["title"] = _title_from_message(title_hint)
    row["updated_at"] = now
    _write_conversation_rows(user_id, rows)
    return cid


def _normalize_agent_config(user_id: str, cfg: dict | None) -> dict:
    base = _default_agent_config(user_id)
    skill_allowed = {row["id"] for row in resources.list_skills(user_id)}
    llm_allowed = {row["id"] for row in resources.list_llms(user_id)}
    mcp_allowed = {
        row["id"]
        for row in resources.list_mcps(user_id)
        if row.get("status") in {"connected", "configured"}
    }
    sandbox_allowed = {row["id"] for row in resources.list_sandboxes(user_id)}
    if not isinstance(cfg, dict):
        return base

    main_in = cfg.get("main_agent") if isinstance(cfg.get("main_agent"), dict) else {}
    main = base["main_agent"]
    main["name"] = str(main_in.get("name") or "主 Agent")[:80]
    main["system_prompt"] = str(main_in.get("system_prompt") or "")
    main["llm_id"] = str(main_in.get("llm_id") or "") if str(main_in.get("llm_id") or "") in llm_allowed else ""
    main["sandbox_id"] = (
        str(main_in.get("sandbox_id") or "")
        if str(main_in.get("sandbox_id") or "") in sandbox_allowed
        else ""
    )
    main["enabled_skills"] = [sid for sid in _as_str_list(main_in.get("enabled_skills")) if sid in skill_allowed]
    main["enabled_mcps"] = [mid for mid in _as_str_list(main_in.get("enabled_mcps")) if mid in mcp_allowed]

    subagents: list[dict] = []
    sub_ids: set[str] = set()
    for idx, raw in enumerate(cfg.get("subagents") or []):
        if not isinstance(raw, dict):
            continue
        sid = _clean_id(str(raw.get("id") or f"sub_{idx + 1}")) or f"sub_{idx + 1}"
        if sid in sub_ids:
            sid = f"{sid}_{idx + 1}"
        sub_ids.add(sid)
        subagents.append(
            {
                "id": sid,
                "name": str(raw.get("name") or f"子 Agent {idx + 1}")[:80],
                "system_prompt": str(raw.get("system_prompt") or ""),
                "llm_id": str(raw.get("llm_id") or "") if str(raw.get("llm_id") or "") in llm_allowed else "",
                "sandbox_id": (
                    str(raw.get("sandbox_id") or "")
                    if str(raw.get("sandbox_id") or "") in sandbox_allowed
                    else ""
                ),
                "enabled_skills": [x for x in _as_str_list(raw.get("enabled_skills")) if x in skill_allowed],
                "enabled_mcps": [x for x in _as_str_list(raw.get("enabled_mcps")) if x in mcp_allowed],
            }
        )

    default_enabled = [sub["id"] for sub in subagents]
    main["enabled_subagents"] = [
        sid for sid in _as_str_list(main_in.get("enabled_subagents") or default_enabled)
        if sid in sub_ids
    ]
    base["subagents"] = subagents
    return base


def get_agent_config(user_id: str) -> dict:
    p = _agent_config_file(user_id)
    if not p.exists():
        return _default_agent_config(user_id)
    try:
        return _normalize_agent_config(user_id, json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return _default_agent_config(user_id)


def save_agent_config(user_id: str, cfg: dict) -> dict:
    normalized = _normalize_agent_config(user_id, cfg)
    _agent_config_file(user_id).write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return normalized


# ---------------------------------------------------------------- Attachments
def list_attachments(user_id: str, conversation_id: str | None = None) -> list[dict]:
    d = attachments_dir(user_id, conversation_id)
    files = []
    for p in sorted(d.iterdir()):
        if p.is_file():
            files.append({"name": p.name, "size": p.stat().st_size, "path": str(p)})
    return files


def clear_attachments(user_id: str, conversation_id: str | None = None) -> int:
    removed = 0
    for p in attachments_dir(user_id, conversation_id).iterdir():
        if p.is_file():
            p.unlink()
            removed += 1
    return removed


def attachment_context(
    user_id: str,
    runtime_attachments_path: str = "",
    conversation_id: str | None = None,
) -> str:
    files = list_attachments(user_id, conversation_id)
    if not files:
        return ""
    names = "、".join(f["name"] for f in files)
    path_hint = runtime_attachments_path or "/attachments"
    return (
        "# 当前会话附件\n"
        f"用户已在当前会话上传附件：{names}。\n"
        f"这些附件已复制到当前 Agent 运行目录：`{path_hint}`。\n"
        "附件是通用对话上下文，不等同于蒸馏阶段的业务数据。需要读取具体文件时，请使用 read_file 读取该目录下的文件。"
    )


def _tool_namespace_hint(*texts: str) -> str:
    blob = "\n".join(str(t or "") for t in texts)
    match = re.search(r"\b(s_[0-9A-Za-z_]+)__[A-Za-z_]+", blob)
    if match:
        return match.group(1)
    match = re.search(r"\bbfe-s-([0-9A-Za-z_]+)\b", blob)
    if match:
        return f"s_{match.group(1)}"
    return ""


# ---------------------------------------------------------------- Chat history
def _msg_id() -> str:
    return f"pgmsg_{uuid.uuid4().hex[:12]}"


def get_messages(user_id: str, conversation_id: str | None = None) -> list[ChatMessage]:
    p = _chat_file(user_id, conversation_id)
    if not p.exists():
        return []
    out: list[ChatMessage] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(ChatMessage(**json.loads(line)))
        except Exception:
            continue
    return out


def _append_message(user_id: str, msg: ChatMessage, conversation_id: str | None = None) -> str:
    cid = _touch_conversation(
        user_id,
        conversation_id or "default",
        msg.content if msg.role == ChatRole.USER else "",
    )
    with _chat_file(user_id, cid).open("a", encoding="utf-8") as f:
        f.write(msg.model_dump_json() + "\n")
    return cid


def clear_messages(user_id: str, conversation_id: str | None = None) -> None:
    cid = _conversation_id(conversation_id)
    p = _chat_file(user_id, cid)
    if p.exists():
        p.unlink()
    rows = _read_conversation_rows(user_id)
    for row in rows:
        if _conversation_id(row.get("id")) == cid:
            row["title"] = "当前对话" if cid == "default" else "新对话"
            row["updated_at"] = time.time()
    _write_conversation_rows(user_id, rows)


def _persist_assistant(
    user_id: str,
    content: str,
    thinking: str = "",
    tools: list[ToolTrace] | None = None,
    conversation_id: str | None = None,
) -> None:
    _append_message(
        user_id,
        ChatMessage(
            id=_msg_id(),
            role=ChatRole.ASSISTANT,
            content=content.strip() or _ABORTED_NOTE,
            thinking=thinking.strip(),
            tools=tools or [],
        ),
        conversation_id,
    )


def _build_lc_history(user_id: str, conversation_id: str | None = None) -> list:
    history = get_messages(user_id, conversation_id)[:-1][-20:]
    lc: list = []
    for m in history:
        if m.role == ChatRole.USER:
            if lc and isinstance(lc[-1], HumanMessage):
                lc.append(AIMessage(content=_ABORTED_NOTE))
            lc.append(HumanMessage(content=m.content))
        elif m.role == ChatRole.ASSISTANT and m.content:
            lc.append(AIMessage(content=m.content))
    if lc and isinstance(lc[-1], HumanMessage):
        lc.append(AIMessage(content=_ABORTED_NOTE))
    return lc


# ---------------------------------------------------------------- Streaming chat
async def _mcp_tools_for(user_id: str, ids: list[str], sandbox: dict | None = None) -> tuple[list, str]:
    connections = resources.mcp_connections(user_id, ids, sandbox=sandbox)
    return await resources.load_mcp_tools(connections)


async def stream_chat(
    user_id: str,
    user_message: str,
    conversation_id: str | None = None,
) -> AsyncIterator[str]:
    conversation_id = _append_message(
        user_id,
        ChatMessage(id=_msg_id(), role=ChatRole.USER, content=user_message),
        conversation_id,
    )

    cfg = get_agent_config(user_id)
    main = cfg.get("main_agent") or {}
    main_llm = resources.create_llm(user_id, main.get("llm_id") or "", streaming=True)
    if main_llm is None:
        msg = "❌ LLM 未配置，无法使用 Agent 平台。请先配置 LLM。"
        _persist_assistant(user_id, msg, conversation_id=conversation_id)
        yield sse("content", delta=msg)
        yield sse_done()
        return

    sandbox_map: dict[str, dict] = {}
    for sandbox_id in resources.sandbox_ids_for_config(cfg):
        row = resources.sandbox_by_id(user_id, sandbox_id)
        if not row:
            continue
        yield sse("status", status=f"正在准备沙箱环境：{row.get('name') or sandbox_id}")
        prepared = await asyncio.to_thread(resources.ensure_sandbox_dependencies, user_id, sandbox_id, cfg)
        if prepared.get("status") != "ready":
            msg = (
                f"❌ 沙箱环境「{prepared.get('name') or sandbox_id}」准备失败："
                f"{prepared.get('error') or '未知错误'}"
            )
            _persist_assistant(user_id, msg, conversation_id=conversation_id)
            yield sse("content", delta=msg)
            yield sse("refresh", resource="resources")
            yield sse_done()
            return
        sandbox_map[sandbox_id] = prepared
    if sandbox_map:
        yield sse("refresh", resource="resources")

    current_attachment_dir = attachments_dir(user_id, conversation_id)
    runtime = resources.prepare_runtime_workspace(
        user_id,
        cfg,
        current_attachment_dir,
    )
    try:
        summary = resources.resource_summary(user_id, cfg)
        main_sandbox = sandbox_map.get(str(main.get("sandbox_id") or ""))
        main_tools, main_mcp_error = await _mcp_tools_for(user_id, main.get("enabled_mcps") or [], main_sandbox)
        runtime_root = Path(runtime["root"])
        main_action_tools = resources.scenario_action_tools(
            user_id,
            main.get("enabled_skills") or [],
            runtime_root / "action_packages" / "main",
            current_attachment_dir,
            runtime_root / "outputs" / "main",
            namespace_hint=_tool_namespace_hint(main.get("system_prompt") or ""),
        )
        main_tools.extend(main_action_tools)
        if main_mcp_error:
            summary.setdefault("main_agent", {})["mcp_load_error"] = main_mcp_error
        if main_action_tools:
            summary.setdefault("main_agent", {})["action_tools"] = [getattr(t, "name", "") for t in main_action_tools]

        enabled_subagents = set(main.get("enabled_subagents") or [])
        summary_by_sub = {
            row.get("id"): row
            for row in summary.get("subagents", [])
            if isinstance(row, dict)
        }
        subagent_specs: list[dict] = []
        has_action_tools = bool(main_action_tools)
        for sub in cfg.get("subagents") or []:
            if not isinstance(sub, dict) or sub.get("id") not in enabled_subagents:
                continue
            sub_sandbox = sandbox_map.get(str(sub.get("sandbox_id") or ""))
            sub_tools, sub_mcp_error = await _mcp_tools_for(user_id, sub.get("enabled_mcps") or [], sub_sandbox)
            sub_action_tools = resources.scenario_action_tools(
                user_id,
                sub.get("enabled_skills") or [],
                runtime_root / "action_packages" / str(sub.get("id") or "subagent"),
                current_attachment_dir,
                runtime_root / "outputs" / str(sub.get("id") or "subagent"),
                namespace_hint=_tool_namespace_hint(sub.get("system_prompt") or "", sub.get("name") or ""),
            )
            sub_tools.extend(sub_action_tools)
            has_action_tools = has_action_tools or bool(sub_action_tools)
            sub_summary = dict(summary_by_sub.get(sub.get("id")) or {})
            if sub_mcp_error:
                sub_summary["mcp_load_error"] = sub_mcp_error
            if sub_action_tools:
                sub_summary["action_tools"] = [getattr(t, "name", "") for t in sub_action_tools]
            skill_source = runtime.get("subagent_skill_sources", {}).get(str(sub.get("id")))
            subagent_specs.append(
                {
                    "config": sub,
                    "model": resources.create_llm(user_id, sub.get("llm_id") or "", streaming=True) or main_llm,
                    "tools": sub_tools,
                    "sandbox_id": str(sub.get("sandbox_id") or ""),
                    "skill_sources": [skill_source] if skill_source else [],
                    "resources_summary": sub_summary,
                }
            )

        agent = build_playground_agent(
            llm=main_llm,
            agent_config=cfg,
            runtime_root=runtime["root"],
            main_skill_sources=runtime.get("main_skill_sources") or [],
            main_tools=main_tools,
            subagent_specs=subagent_specs,
            sandbox_map=sandbox_map,
            main_sandbox_id=str(main.get("sandbox_id") or ""),
            resources_summary=summary,
            disable_execute=has_action_tools,
            attachment_context=attachment_context(
                user_id,
                runtime.get("attachments_path") or "",
                conversation_id,
            ),
        )
    except RuntimeError as exc:
        msg = f"❌ 无法构建 Agent 平台主 Agent：{exc}"
        _persist_assistant(user_id, msg, conversation_id=conversation_id)
        resources.cleanup_runtime_workspace(runtime.get("root", ""))
        yield sse("content", delta=msg)
        yield sse_done()
        return
    except Exception as exc:  # noqa: BLE001
        msg = f"❌ Agent 平台资源加载失败：{type(exc).__name__}: {exc}"
        _persist_assistant(user_id, msg, conversation_id=conversation_id)
        resources.cleanup_runtime_workspace(runtime.get("root", ""))
        yield sse("content", delta=msg)
        yield sse_done()
        return

    lc_history = _build_lc_history(user_id, conversation_id)

    def _persist(content: str, thinking: str, tools: list[ToolTrace]) -> None:
        _persist_assistant(user_id, content, thinking, tools, conversation_id=conversation_id)

    try:
        async for frame in stream_agent(
            agent,
            lc_history,
            user_message,
            _persist,
            aborted_note=_ABORTED_NOTE,
        ):
            yield frame
        yield sse_done()
    finally:
        resources.cleanup_runtime_workspace(runtime.get("root", ""))


# ---------------------------------------------------------------- Release helpers
def public_base_url(request) -> str:
    env = settings.mcp_base_url
    if env:
        return env
    return str(request.base_url).rstrip("/")


def _pkg_for(scenario_id: str) -> rt.ScenarioPackage | None:
    try:
        release = ensure_release_package(scenario_id)
        pkg_dir = release.package_dir
    except Exception:
        return None
    if not (Path(pkg_dir) / "main_skill").exists():
        return None
    return rt.ScenarioPackage.load(pkg_dir)


def build_install_config(scenario_id: str, base_url: str) -> dict:
    pkg = _pkg_for(scenario_id)
    if not pkg:
        return {}
    release = release_status(scenario_id, base_url=base_url)
    scenario = store.get(scenario_id)
    scenario_status = scenario.status.value if scenario else ""
    publish_allowed = scenario is not None and scenario.status == ScenarioStatus.ACTIVE
    namespace = pkg.namespace
    base = base_url.rstrip("/")
    sse_url = f"{base}/api/mcp/{scenario_id}/sse"
    token = settings.mcp_access_token.strip()
    key = f"bfe-{namespace}"
    skill_name = pkg.card.get("skill_name") or f"bfe-{namespace.replace('_', '-')}"
    skill_dir = Path(release.get("skill_dir") or "")
    system_prompt_path = skill_dir / "system_prompt.md"
    system_prompt = ""
    child_skills: list[dict] = []

    try:
        if system_prompt_path.exists():
            system_prompt = system_prompt_path.read_text(encoding="utf-8")
    except Exception:
        system_prompt = ""

    try:
        if skill_dir.exists():
            for child in sorted(skill_dir.iterdir()):
                md = child / "SKILL.md"
                if not child.is_dir() or not md.exists():
                    continue
                label = child.name
                try:
                    text = md.read_text(encoding="utf-8")
                    m = re.search(r"(?m)^name:\s*([^\n]+)$", text)
                    if m:
                        label = m.group(1).strip().strip('"')
                except Exception:
                    pass
                child_skills.append({"skill_id": child.name, "name": label})
    except Exception:
        child_skills = []

    remote_args = ["-y", "mcp-remote", sse_url]
    native_entry: dict = {"url": sse_url}
    if token:
        remote_args += ["--header", f"Authorization: Bearer {token}"]
        native_entry["headers"] = {"Authorization": f"Bearer {token}"}

    return {
        "sse_url": sse_url,
        "base_url": base,
        "base_from_env": bool(settings.mcp_base_url),
        "requires_token": bool(token),
        "scenario_status": scenario_status,
        "publish_allowed": publish_allowed,
        "publish_block_reason": "" if publish_allowed else (
            f"当前场景状态为 {scenario_status or 'unknown'}，尚未记录为验证通过。"
        ),
        "skill_install": {
            "skill_name": skill_name,
            "source_dir": str(skill_dir) if skill_dir.exists() else str(Path(store.skills_dir(scenario_id)).resolve()),
            "skill_zip": release.get("downloads", {}).get("skill_zip", ""),
            "toolplane_docker_zip": release.get("downloads", {}).get("toolplane_docker_zip", ""),
            "mcp_zip": release.get("downloads", {}).get("mcp_zip", ""),
            "subagent_prompt_file": str(system_prompt_path) if system_prompt_path.exists() else "system_prompt.md",
            "toolkit_file": "",
            "subagent_system_prompt": system_prompt,
            "child_skills": child_skills,
            "codex_target_hint": f"~/.codex/skills/{skill_name}",
            "install_summary": "标准 Skill、MCP 与 Docker 发布物已生成，可复制到第三方 Agent 平台使用。",
            "default_prompt": f"Use ${skill_name} to inspect my business data and complete this scenario task.",
        },
        "release": release,
        "config_example": {"mcpServers": {key: {"command": "npx", "args": remote_args}}},
        "config_example_native": {"mcpServers": {key: native_entry}},
    }


def mount_config(scenario_id: str, base_url: str) -> dict:
    pkg = _pkg_for(scenario_id)
    if not pkg:
        return {}
    install = build_install_config(scenario_id, base_url)
    return {
        "scenario_id": scenario_id,
        "card": pkg.card,
        **install,
        "skills_dir": str(Path(store.skills_dir(scenario_id)).resolve()),
        "release_dir": install.get("release", {}).get("package_dir", ""),
        "generated_at": time.time(),
    }
