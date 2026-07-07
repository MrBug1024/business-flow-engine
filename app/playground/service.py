"""通用第三方沙盒服务：挂载管理 + 会话历史 + 流式对话（按用户隔离）。

每个用户一套独立的沙盒状态，存放在 data/playground/<user_id>/ 下：
    mounts.json          该用户已挂载的场景 id 列表
    chat.jsonl           该用户的沙盒对话历史

「挂载」= 第三方把某个业务能力包接入自己的 Agent。推荐形态是标准 Skill；
MCP 配置仅作为支持工具调用宿主的兼容方式。
用户只能挂载/操作自己拥有的场景能力包。
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage

from app.core.agent_stream import stream_agent
from app.core.config import settings
from app.core.llm import get_llm
from app.domain.models import ChatMessage, ChatRole, ScenarioStatus, ToolTrace
from app.release.builder import ensure_release_package, release_status
from app.playground.agent import build_playground_agent
from app.domain.storage import store
from app.runtime import scenario_runtime as rt
from app.core.streaming import sse, sse_done
from app.verification.state import response_marks_verified

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
    try:
        release = ensure_release_package(scenario_id)
        pkg_dir = release.package_dir
    except Exception:
        return None
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


def public_base_url(request) -> str:
    """解析对外基址：优先用 .env 的固定域名；未配置则按本次请求的主机地址推导。

    这样开发/测试无需任何配置，第三方就能用「访问本服务所用的地址」安装；
    正式环境在 .env 配 MCP_PUBLIC_BASE_URL 即切换到固定域名。
    """
    env = settings.mcp_base_url
    if env:
        return env
    return str(request.base_url).rstrip("/")


def build_install_config(scenario_id: str, base_url: str) -> dict:
    """生成 Skill-only 与 MCP-only 安装配置。

    返回：
    - skill_install：标准 Skill 目录/zip 与 system_prompt.md；
    - config_example/config_example_native：MCP-only 配置，供支持 MCP 的宿主使用。
    MCP 配置包含两种形态：
    - config_example：走 `mcp-remote` 桥接，兼容仅支持 stdio 的宿主（如 Claude Desktop）
    - config_example_native：直接给 `url`，供原生支持远程 MCP 的宿主（Cursor / Cline 等）
    """
    pkg = _pkg_for(scenario_id)
    if not pkg:
        return {}
    release = release_status(scenario_id, base_url=base_url)
    scenario = store.get(scenario_id)
    scenario_status = scenario.status.value if scenario else ""
    publish_allowed = scenario is not None and scenario.status == ScenarioStatus.ACTIVE
    ns = pkg.namespace
    base = base_url.rstrip("/")
    sse_url = f"{base}/api/mcp/{scenario_id}/sse"
    token = settings.mcp_access_token.strip()
    key = f"bfe-{ns}"
    skill_name = pkg.card.get("skill_name") or f"bfe-{ns.replace('_', '-')}"
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
                name = child.name
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
            "install_summary": "推荐按标准 Skill 安装：导入 skill.zip 或复制 skill/ 目录；MCP 请使用单独的 mcp.zip/Docker 包。",
            "default_prompt": f"Use ${skill_name} to inspect my business data and complete this scenario task.",
        },
        "release": release,
        "config_example": {"mcpServers": {key: {"command": "npx", "args": remote_args}}},
        "config_example_native": {"mcpServers": {key: native_entry}},
    }


def mount_config(scenario_id: str, base_url: str) -> dict:
    """返回某场景的能力卡片 + 面向第三方的远程 MCP 安装配置（配置面板用）。"""
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
    }


def _mark_single_mounted_scenario_verified(user_id: str, content: str) -> None:
    """Sandbox 验证结论写回场景状态，避免验证和发布状态脱节。"""
    if not response_marks_verified(content):
        return
    owned_ids = [sid for sid in get_mounts(user_id) if _owns(user_id, sid)]
    if len(owned_ids) != 1:
        return
    scenario = store.get(owned_ids[0])
    if scenario and scenario.status == ScenarioStatus.SKILLS_GENERATED:
        scenario.status = ScenarioStatus.ACTIVE
        store.save(scenario)


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
        _mark_single_mounted_scenario_verified(user_id, content)

    async for frame in stream_agent(
        agent, lc_history, user_message, _persist, aborted_note=_ABORTED_NOTE,
    ):
        yield frame
    yield sse_done()
