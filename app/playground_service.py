"""通用第三方沙盒服务：挂载管理 + 会话历史 + 流式对话（按用户隔离）。

每个用户一套独立的沙盒状态，存放在 data/playground/<user_id>/ 下：
    mounts.json          该用户已挂载的场景 id 列表
    chat.jsonl           该用户的沙盒对话历史

「挂载」= 第三方把某个业务能力包接入自己的 Agent（等价于在 MCP 配置里加一条 server）。
用户只能挂载/操作自己拥有的场景能力包。
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage

from .agent_stream import stream_agent
from .config import settings
from .llm import get_llm
from .models import ChatMessage, ChatRole, ToolTrace
from . import scenario_runtime as rt
from .playground_agent import build_playground_agent
from .storage import store
from .streaming import sse, sse_done

_ABORTED_NOTE = "（本轮执行被中断，任务已终止作废，后续消息将作为全新任务独立处理。）"


def _safe(uid: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", uid or "anon")


def _pg_dir(user_id: str) -> Path:
    d = settings.data_path / "playground" / _safe(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _mounts_file(user_id: str) -> Path:
    return _pg_dir(user_id) / "mounts.json"


def _chat_file(user_id: str) -> Path:
    return _pg_dir(user_id) / "chat.jsonl"


# ---------------------------------------------------------------- 挂载管理
def get_mounts(user_id: str) -> list[str]:
    p = _mounts_file(user_id)
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text(encoding="utf-8")).get("mounts", []))
    except Exception:
        return []


def _save_mounts(user_id: str, ids: list[str]) -> None:
    _mounts_file(user_id).write_text(
        json.dumps({"mounts": ids}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _owns(user_id: str, scenario_id: str) -> bool:
    sc = store.get(scenario_id)
    return sc is not None and sc.owner_id == user_id


def mount(user_id: str, scenario_id: str) -> list[str]:
    ids = get_mounts(user_id)
    if scenario_id not in ids:
        ids.append(scenario_id)
        _save_mounts(user_id, ids)
    return ids


def unmount(user_id: str, scenario_id: str) -> list[str]:
    ids = [i for i in get_mounts(user_id) if i != scenario_id]
    _save_mounts(user_id, ids)
    return ids


def _pkg_for(scenario_id: str) -> rt.ScenarioPackage | None:
    pkg_dir = store.skills_dir(scenario_id)
    if not (Path(pkg_dir) / "main_skill").exists():
        return None
    return rt.ScenarioPackage.load(pkg_dir)


def mounted_packages(user_id: str) -> list[rt.ScenarioPackage]:
    pkgs = []
    for sid in get_mounts(user_id):
        if not _owns(user_id, sid):
            continue
        p = _pkg_for(sid)
        if p and p.is_ready():
            pkgs.append(p)
    return pkgs


def catalog(user_id: str) -> list[dict]:
    """能力市场：该用户拥有且已生成能力包的场景（可安装项），标注是否已挂载。"""
    mounts = set(get_mounts(user_id))
    items: list[dict] = []
    for sc in store.list(owner_id=user_id):
        pkg = _pkg_for(sc.id)
        if not pkg or not pkg.card:
            continue
        items.append({
            "scenario_id": sc.id,
            "namespace": pkg.namespace,
            "display_name": pkg.display_name,
            "summary": pkg.summary,
            "when_to_use": pkg.when_to_use,
            "not_for": pkg.not_for,
            "tools": [t.get("name") for t in pkg.tools],
            "mounted": sc.id in mounts,
        })
    return items


def mount_config(scenario_id: str) -> dict:
    """返回某场景的能力卡片 + 粘贴即用的 MCP 配置片段（配置面板用）。"""
    pkg = _pkg_for(scenario_id)
    if not pkg:
        return {}
    cfg_file = Path(store.skills_dir(scenario_id)) / "mcp_config.example.json"
    config_example = {}
    if cfg_file.exists():
        try:
            config_example = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            config_example = {}
    return {
        "scenario_id": scenario_id,
        "card": pkg.card,
        "config_example": config_example,
        "skills_dir": str(Path(store.skills_dir(scenario_id)).resolve()),
    }


# ---------------------------------------------------------------- 会话历史
def _msg_id() -> str:
    return f"pgmsg_{uuid.uuid4().hex[:12]}"


def get_messages(user_id: str) -> list[ChatMessage]:
    p = _chat_file(user_id)
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


def _append_message(user_id: str, msg: ChatMessage) -> None:
    with _chat_file(user_id).open("a", encoding="utf-8") as f:
        f.write(msg.model_dump_json() + "\n")


def clear_messages(user_id: str) -> None:
    p = _chat_file(user_id)
    if p.exists():
        p.unlink()


def _persist_assistant(user_id: str, content: str, thinking: str = "",
                       tools: list[ToolTrace] | None = None) -> None:
    _append_message(user_id, ChatMessage(
        id=_msg_id(), role=ChatRole.ASSISTANT,
        content=content.strip() or _ABORTED_NOTE, thinking=thinking.strip(), tools=tools or [],
    ))


def _build_lc_history(user_id: str) -> list:
    history = get_messages(user_id)[:-1][-20:]  # 去掉刚追加的本轮用户消息
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


# ---------------------------------------------------------------- 流式对话
async def stream_chat(user_id: str, user_message: str) -> AsyncIterator[str]:
    _append_message(user_id, ChatMessage(id=_msg_id(), role=ChatRole.USER, content=user_message))

    if get_llm() is None:
        msg = "❌ LLM 未配置，无法使用沙盒。请先配置 LLM。"
        _persist_assistant(user_id, msg)
        yield sse("content", delta=msg)
        yield sse_done()
        return

    pkgs = mounted_packages(user_id)
    try:
        agent = build_playground_agent(pkgs)
    except RuntimeError as exc:
        msg = f"❌ 无法构建沙盒 Agent：{exc}"
        _persist_assistant(user_id, msg)
        yield sse("content", delta=msg)
        yield sse_done()
        return

    lc_history = _build_lc_history(user_id)

    def _persist(content: str, thinking: str, tools: list[ToolTrace]) -> None:
        _persist_assistant(user_id, content, thinking, tools)

    async for frame in stream_agent(
        agent, lc_history, user_message, _persist, aborted_note=_ABORTED_NOTE,
    ):
        yield frame
    yield sse_done()
