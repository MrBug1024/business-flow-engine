"""第三方发布包构建器。

蒸馏阶段生成的 ``data/scenarios/<id>/skills`` 是平台内部工作产物；第三方需要的
是可复制、可下载、可 Docker 化、无本机绝对路径的发布包。本模块把内部技能目录
转换为稳定的 release 包，并让验证沙盒也从 release 包加载，确保“验证通过”和
“第三方安装后可用”走同一套目录结构。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.storage import store


_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _ROOT / "app"
_BUILDER_VERSION = "1.7"
_DEFAULT_DOCKER_REGISTRY = "harbor.gshbzw.com/skills"
_DEFAULT_IMAGE_TAG = "1.0.0"
_IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", "outputs"}
_IGNORED_SUFFIXES = {".pyc", ".pyo", ".cache.pkl"}

_PINYIN_PHRASES = [
    ("医疗保险", "yiliao-baoxian"),
    ("医保", "yibao"),
    ("医疗", "yiliao"),
    ("保险", "baoxian"),
    ("审计", "shenji"),
    ("审核", "shenhe"),
    ("审查", "shencha"),
    ("风控", "fengkong"),
    ("风险", "fengxian"),
    ("财务", "caiwu"),
    ("合同", "hetong"),
    ("报销", "baoxiao"),
    ("结算", "jiesuan"),
    ("收费", "shoufei"),
    ("采购", "caigou"),
    ("库存", "kucun"),
    ("销售", "xiaoshou"),
    ("客户", "kehu"),
    ("订单", "dingdan"),
    ("项目", "xiangmu"),
    ("规则", "guize"),
    ("知识", "zhishi"),
    ("质量", "zhiliang"),
    ("安全", "anquan"),
    ("人事", "renshi"),
    ("行政", "xingzheng"),
]

_PINYIN_CHARS = {
    "医": "yi", "疗": "liao", "保": "bao", "险": "xian", "审": "shen", "计": "ji",
    "核": "he", "查": "cha", "风": "feng", "控": "kong", "财": "cai", "务": "wu",
    "合": "he", "同": "tong", "报": "bao", "销": "xiao", "结": "jie", "算": "suan",
    "收": "shou", "费": "fei", "采": "cai", "购": "gou", "库": "ku", "存": "cun",
    "售": "shou", "客": "ke", "户": "hu", "订": "ding", "单": "dan", "项": "xiang",
    "目": "mu", "规": "gui", "则": "ze", "知": "zhi", "识": "shi", "质": "zhi",
    "量": "liang", "安": "an", "全": "quan", "人": "ren", "事": "shi", "行": "xing",
    "政": "zheng", "业": "ye", "务": "wu", "场": "chang", "景": "jing", "数": "shu",
    "据": "ju", "表": "biao", "流": "liu", "程": "cheng", "分": "fen", "析": "xi",
    "管": "guan", "理": "li", "监": "jian", "测": "ce", "预": "yu", "警": "jing",
    "评": "ping", "估": "gu", "资": "zi", "产": "chan", "供": "gong", "应": "ying",
    "链": "lian", "仓": "cang", "储": "chu", "物": "wu", "流": "liu", "运": "yun",
    "营": "ying", "维": "wei", "护": "hu", "服": "fu", "市": "shi", "营": "ying",
    "销": "xiao", "商": "shang", "品": "pin", "账": "zhang", "款": "kuan", "票": "piao",
    "税": "shui", "成": "cheng", "本": "ben", "利": "li", "润": "run", "绩": "ji",
    "效": "xiao", "员": "yuan", "工": "gong", "考": "kao", "勤": "qin", "薪": "xin",
    "酬": "chou", "招": "zhao", "聘": "pin", "培": "pei", "训": "xun",
}


@dataclass(frozen=True)
class ReleaseBuild:
    scenario_id: str
    skill_name: str
    package_dir: Path
    artifacts_dir: Path
    skill_zip: Path
    docker_zip: Path
    manifest_path: Path
    warnings: list[str]

    def as_dict(self, base_url: str = "") -> dict[str, Any]:
        base = base_url.rstrip("/")
        prefix = f"{base}/api/scenarios/{self.scenario_id}/release/download" if base else ""
        downloads = {
            "skill_zip": f"{prefix}/skill.zip" if prefix else str(self.skill_zip),
            "toolplane_docker_zip": f"{prefix}/toolplane-docker.zip" if prefix else str(self.docker_zip),
        }
        registry = _docker_registry()
        docker_image = f"{registry}/{self.skill_name}:{_DEFAULT_IMAGE_TAG}"
        start_command = "python -m bfe_runtime.mcp_server --pkg /app"
        return {
            "scenario_id": self.scenario_id,
            "skill_name": self.skill_name,
            "package_dir": str(self.package_dir),
            "artifacts_dir": str(self.artifacts_dir),
            "downloads": downloads,
            "install_modes": {
                "skill_directory": {
                    "title": "标准 Skill 目录/zip",
                    "entry": "SKILL.md",
                    "artifact": "skill.zip",
                    "note": "适用于支持本地 Skill 目录、zip 导入或 GitHub Skill 同步的宿主。",
                },
                "toolplane_docker": {
                    "title": "ToolPlane / Docker MCP",
                    "artifact": "toolplane-docker.zip",
                    "docker_image": docker_image,
                    "start_command": start_command,
                    "server_name": self.skill_name,
                    "repository": self.skill_name,
                    "tag": _DEFAULT_IMAGE_TAG,
                    "registry": registry,
                    "build_command": f"docker build -t {self.skill_name}:{_DEFAULT_IMAGE_TAG} .",
                    "tag_command": f"docker tag {self.skill_name}:{_DEFAULT_IMAGE_TAG} {docker_image}",
                    "push_command": f"docker push {docker_image}",
                    "note": "适用于支持 Docker source 的 MCP 平台。构建镜像后以 stdio MCP Server 运行。",
                },
                "mcp_stdio": {
                    "title": "通用 stdio MCP",
                    "command": "python",
                    "args": [str(self.package_dir / "run_mcp.py")],
                    "note": "适用于支持 command/args 的本地 MCP 宿主。",
                },
            },
            "warnings": self.warnings,
        }


def ensure_release_package(scenario_id: str, base_url: str = "") -> ReleaseBuild:
    """确保 release 包存在并且比内部 skills 新。

    为了避免验证通道偷吃内部目录，这个函数会在沙盒 catalog/mount 时自动构建。
    """
    src = Path(store.skills_dir(scenario_id))
    release_base = Path(store.release_dir(scenario_id))
    skill_name = _skill_name(src, scenario_id)
    package_dir = release_base / skill_name
    manifest_path = release_base / "release.json"
    source_hash = _tree_hash(src)

    if manifest_path.exists() and package_dir.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                manifest.get("source_hash") == source_hash
                and manifest.get("builder_version") == _BUILDER_VERSION
            ):
                artifacts = release_base / "artifacts"
                return ReleaseBuild(
                    scenario_id=scenario_id,
                    skill_name=skill_name,
                    package_dir=package_dir,
                    artifacts_dir=artifacts,
                    skill_zip=artifacts / "skill.zip",
                    docker_zip=artifacts / "toolplane-docker.zip",
                    manifest_path=manifest_path,
                    warnings=list(manifest.get("warnings", [])),
                )
        except Exception:
            pass
    return build_release_package(scenario_id, base_url=base_url, source_hash=source_hash)


def build_release_package(
    scenario_id: str,
    base_url: str = "",
    source_hash: str | None = None,
) -> ReleaseBuild:
    """从内部 skills/ 目录构建第三方发布包。"""
    src = Path(store.skills_dir(scenario_id))
    if not (src / "SKILL.md").exists() or not (src / "mcp.json").exists():
        raise FileNotFoundError("该场景尚未生成可发布的能力包（缺少 SKILL.md 或 mcp.json）。")

    release_base = Path(store.release_dir(scenario_id))
    skill_name = _skill_name(src, scenario_id)
    package_dir = release_base / skill_name
    artifacts_dir = release_base / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_release_packages(release_base, keep=package_dir)

    if package_dir.exists():
        shutil.rmtree(package_dir)
    _copy_tree(src, package_dir)

    warnings: list[str] = []
    _normalize_release_package(package_dir, skill_name, warnings)
    _write_runtime(package_dir)
    _write_docker_files(package_dir, skill_name)
    _write_install_docs(package_dir, skill_name)
    _write_release_mcp_config(package_dir, skill_name)
    warnings.extend(_validate_release_package(package_dir))

    skill_zip = artifacts_dir / "skill.zip"
    docker_zip = artifacts_dir / "toolplane-docker.zip"
    _zip_dir(package_dir, skill_zip, root_name=skill_name, exclude_runtime=True)
    _zip_dir(package_dir, docker_zip, root_name=skill_name, exclude_runtime=False)

    build = ReleaseBuild(
        scenario_id=scenario_id,
        skill_name=skill_name,
        package_dir=package_dir,
        artifacts_dir=artifacts_dir,
        skill_zip=skill_zip,
        docker_zip=docker_zip,
        manifest_path=release_base / "release.json",
        warnings=warnings,
    )
    manifest = {
        **build.as_dict(base_url=base_url),
        "source_hash": source_hash or _tree_hash(src),
        "package_format_version": "1.0",
        "builder_version": _BUILDER_VERSION,
        "artifact_files": {
            "skill_zip": str(skill_zip),
            "toolplane_docker_zip": str(docker_zip),
        },
    }
    build.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return build


def release_status(scenario_id: str, base_url: str = "") -> dict[str, Any]:
    build = ensure_release_package(scenario_id, base_url=base_url)
    status = build.as_dict(base_url=base_url)
    status["ready"] = build.skill_zip.exists() and build.docker_zip.exists()
    return status


def publish_docker_image(
    scenario_id: str,
    *,
    registry: str = "",
    repository: str = "",
    tag: str = "",
    base_url: str = "",
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """构建、标记并推送 Docker MCP 镜像。

    等价于：
      docker build -t REPOSITORY:TAG .
      docker tag REPOSITORY:TAG harbor.gshbzw.com/skills/REPOSITORY:TAG
      docker push harbor.gshbzw.com/skills/REPOSITORY:TAG
    """
    build = ensure_release_package(scenario_id, base_url=base_url)
    registry = _safe_registry(registry or _docker_registry())
    repository = _safe_repository(repository or build.skill_name)
    tag = _safe_tag(tag or _DEFAULT_IMAGE_TAG)

    local_image = f"{repository}:{tag}"
    target_image = f"{registry}/{repository}:{tag}"
    steps = [
        ("build", ["docker", "build", "-t", local_image, "."]),
        ("tag", ["docker", "tag", local_image, target_image]),
        ("push", ["docker", "push", target_image]),
    ]

    if shutil.which("docker") is None:
        return {
            "ok": False,
            "image": target_image,
            "error": "当前服务器未找到 docker 命令，无法自动发布镜像。",
            "steps": [],
        }

    logs: list[dict[str, Any]] = []
    info = _run_step("docker-info", ["docker", "info"], cwd=build.package_dir, timeout_seconds=30)
    if info["returncode"] != 0:
        logs.append(info)
        return {
            "ok": False,
            "image": target_image,
            "repository": repository,
            "tag": tag,
            "registry": registry,
            "failed_step": "docker-info",
            "error": _docker_daemon_error(info),
            "steps": logs,
        }

    for name, cmd in steps:
        result = _run_step(name, cmd, cwd=build.package_dir, timeout_seconds=timeout_seconds)
        logs.append(result)
        if result["returncode"] != 0:
            return {
                "ok": False,
                "image": target_image,
                "repository": repository,
                "tag": tag,
                "registry": registry,
                "failed_step": name,
                "error": _docker_step_error(name, result, registry),
                "steps": logs,
            }

    return {
        "ok": True,
        "image": target_image,
        "repository": repository,
        "tag": tag,
        "registry": registry,
        "start_command": "python -m bfe_runtime.mcp_server --pkg /app",
        "server_name": repository,
        "steps": logs,
    }


def artifact_path(scenario_id: str, artifact: str) -> Path:
    build = ensure_release_package(scenario_id)
    name = artifact.strip().lower()
    if name in {"skill", "skill.zip"}:
        return build.skill_zip
    if name in {"toolplane-docker", "toolplane-docker.zip", "docker", "docker.zip"}:
        return build.docker_zip
    raise FileNotFoundError(f"未知发布物：{artifact}")


def _run_step(name: str, cmd: list[str], cwd: Path, timeout_seconds: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "name": name,
            "command": " ".join(cmd),
            "returncode": proc.returncode,
            "stdout": _decode_process_output(proc.stdout),
            "stderr": _decode_process_output(proc.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "command": " ".join(cmd),
            "returncode": 124,
            "stdout": _decode_process_output(exc.stdout),
            "stderr": f"{name} 超过 {timeout_seconds}s 未完成，已终止。",
        }
    except Exception as exc:
        return {
            "name": name,
            "command": " ".join(cmd),
            "returncode": 1,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }


def _decode_process_output(value: bytes | str | None, limit: int = 12000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[-limit:]
    for encoding in ("utf-8", "gbk"):
        try:
            return value.decode(encoding)[-limit:]
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")[-limit:]


def _docker_daemon_error(info: dict[str, Any]) -> str:
    detail = (info.get("stderr") or info.get("stdout") or "").strip()
    lower = detail.lower()
    if "dockerdesktoplinuxengine" in lower or "docker_engine" in lower or "daemon is running" in lower:
        return (
            "Docker Engine 未运行或当前后端进程无法访问 Docker daemon。"
            "请在发布机启动 Docker Desktop，并确认使用 Linux containers 后重试。"
        )
    if "access is denied" in lower or "permission denied" in lower:
        return "当前后端进程没有访问 Docker 配置或 Docker daemon 的权限，请调整运行账号权限后重试。"
    return "Docker 预检失败，当前发布机暂时不能构建镜像。"


def _docker_step_error(name: str, result: dict[str, Any], registry: str) -> str:
    detail = (result.get("stderr") or result.get("stdout") or "").strip()
    lower = detail.lower()
    registry_host = registry.split("/", 1)[0]
    if name == "push":
        if "no basic auth credentials" in lower or "authorization failed" in lower:
            return (
                f"Docker 镜像已构建并标记成功，但推送到 {registry_host} 失败："
                "当前后端运行账号没有 Harbor 登录凭据。"
                f"请在启动后端的同一个终端/账号下执行 docker login {registry_host} 后重试。"
            )
        if "repository does not exist" in lower:
            return (
                f"Docker 镜像已构建并标记成功，但 Harbor 仓库/项目不存在或当前账号无权推送：{registry}。"
                "请确认 Harbor 中已存在对应项目，并给当前账号分配推送权限。"
            )
        if "denied" in lower or "unauthorized" in lower or "forbidden" in lower:
            return (
                f"Docker 镜像已构建并标记成功，但当前 Harbor 账号无权推送到 {registry}。"
                "请检查项目权限、机器人账号权限或镜像仓库命名。"
            )
    return detail or f"{name} 执行失败"


def _skill_name(src: Path, scenario_id: str) -> str:
    card = _jload(src / "mcp.json") or {}
    manifest = _jload(src / "manifest.json") or {}
    raw = (
        manifest.get("scenario_name")
        or card.get("display_name")
        or card.get("summary")
        or card.get("skill_name")
        or card.get("agent_skill", {}).get("name")
        or scenario_id
    )
    name = _scenario_name_slug(str(raw))
    if not name:
        suffix = scenario_id.split("_")[-1][-12:]
        name = f"scenario-{suffix}"
    return _safe_skill_name(name)


def _safe_skill_name(name: str) -> str:
    name = name.strip().lower().replace("_", "-")
    name = re.sub(r"[^a-z0-9-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name[:64] or "bfe-scenario"


def _docker_registry() -> str:
    return (os.getenv("BFE_DOCKER_REGISTRY") or _DEFAULT_DOCKER_REGISTRY).strip().strip("/")


def _safe_registry(value: str) -> str:
    value = re.sub(r"^https?://", "", (value or "").strip().strip("/"))
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+", value):
        raise ValueError(f"非法 Docker registry：{value!r}")
    return value


def _safe_repository(value: str) -> str:
    value = (value or "").strip().lower().replace("_", "-")
    value = re.sub(r"[^a-z0-9./-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-/.")
    if not value or not re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", value):
        raise ValueError(f"非法 Docker repository：{value!r}")
    return value[:120]


def _safe_tag(value: str) -> str:
    value = (value or _DEFAULT_IMAGE_TAG).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise ValueError(f"非法 Docker tag：{value!r}")
    return value


def _scenario_name_slug(name: str) -> str:
    """把业务场景名转成适合作为 Docker repository / Skill 目录的英文名。

    优先级：
    1. 名称里已有英文/数字，则直接用英文 slug；
    2. 安装了 pypinyin 时使用完整拼音；
    3. 未安装 pypinyin 时，用内置常用业务汉字拼音表兜底；
    4. 仍无法转换的字符丢弃，最后由调用方回退 scenario id。
    """
    text = (name or "").strip().lower()
    ascii_words = re.findall(r"[a-z0-9]+", text)
    if ascii_words:
        return "-".join(ascii_words[:8])

    pinyin = _pinyin_slug(text)
    if pinyin:
        return pinyin
    return ""


def _pinyin_slug(text: str) -> str:
    try:
        from pypinyin import lazy_pinyin  # type: ignore

        parts = [p for p in lazy_pinyin(text, errors="ignore") if p]
        if parts:
            return "-".join(parts)
    except Exception:
        pass

    tokens: list[str] = []
    i = 0
    while i < len(text):
        matched = False
        for phrase, py in _PINYIN_PHRASES:
            if text.startswith(phrase, i):
                tokens.append(py)
                i += len(phrase)
                matched = True
                break
        if matched:
            continue
        ch = text[i]
        if ch in _PINYIN_CHARS:
            tokens.append(_PINYIN_CHARS[ch])
        elif ch.isascii() and ch.isalnum():
            tokens.append(ch.lower())
        i += 1
    return "-".join(t for t in tokens if t)


def _jload(path: Path) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _copy_tree(src: Path, dst: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for name in names:
            p = Path(name)
            if name in _IGNORED_DIRS or any(name.endswith(suf) for suf in _IGNORED_SUFFIXES):
                ignored.add(name)
            elif p.name.startswith(".") and p.name not in {".well-known"}:
                ignored.add(name)
        return ignored

    shutil.copytree(src, dst, ignore=ignore)


def _cleanup_stale_release_packages(release_base: Path, keep: Path) -> None:
    """Remove old generated package directories under release/, keeping artifacts."""
    release_base = release_base.resolve()
    keep = keep.resolve()
    if not release_base.exists():
        return
    for child in release_base.iterdir():
        if not child.is_dir():
            continue
        resolved = child.resolve()
        if resolved == keep or child.name == "artifacts":
            continue
        if release_base not in resolved.parents:
            continue
        if (child / "mcp.json").exists() and (child / "run_mcp.py").exists():
            shutil.rmtree(child)


def _normalize_release_package(package_dir: Path, skill_name: str, warnings: list[str]) -> None:
    mcp_path = package_dir / "mcp.json"
    card = _jload(mcp_path) or {}

    card.pop("server", None)
    card["skill_name"] = skill_name
    card["server_stdio_fallback"] = {
        "transport": "stdio",
        "command": "python",
        "args": ["run_mcp.py"],
        "note": "在发布包根目录运行；不依赖蒸馏平台代码。",
    }
    card["primary_install_mode"] = "toolplane_docker"
    card["agent_skill"] = {
        "name": skill_name,
        "install": "将发布包根目录复制/解压到宿主 skills 目录，或通过 GitHub/zip 同步。",
        "entry": "SKILL.md",
        "primary": True,
    }
    card["release"] = {
        "format": "bfe-universal-scenario-package",
        "root": ".",
        "runtime": "bfe_runtime",
        "detached_from_platform": True,
        "install_modes": ["skill_directory", "toolplane_docker", "mcp_stdio", "github_source"],
    }
    _ensure_release_tool_schema(card)
    _write_capability_docs(package_dir, card)
    mcp_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")

    req = package_dir / "requirements.txt"
    existing = req.read_text(encoding="utf-8") if req.exists() else ""
    if "mcp" not in {line.strip().split("==")[0].split(">=")[0] for line in existing.splitlines()}:
        existing = existing.rstrip() + "\n\n# MCP Server runtime\nmcp>=1.2.0\n"
    req.write_text(existing.lstrip(), encoding="utf-8")

    skill_md = package_dir / "SKILL.md"
    if skill_md.exists():
        text = skill_md.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^name:\s*.+$", f"name: {skill_name}", text, count=1)
        text = text.replace(
            "若宿主支持 MCP，可使用 `mcp_config.example.json` 挂载平台托管调试端点；正式离线使用时优先按本 Skill 的脚本入口运行。",
            "若宿主支持 MCP，可使用 `mcp_config.stdio.example.json` 或 Dockerfile 启动本发布包内置 MCP Server；无需回连蒸馏平台。",
        )
        skill_md.write_text(text, encoding="utf-8")

    openai_yaml = package_dir / "agents" / "openai.yaml"
    if openai_yaml.exists():
        text = openai_yaml.read_text(encoding="utf-8")
        text = re.sub(
            r"(?m)^  default_prompt:\s*.+$",
            f'  default_prompt: "Use ${skill_name} to inspect my business data and complete this scenario task."',
            text,
            count=1,
        )
        openai_yaml.write_text(text, encoding="utf-8")

    if _contains_platform_path(package_dir):
        warnings.append("发布包中仍检测到平台路径或 app.*.mcp_server 引用，请检查生成内容。")


def _write_capability_docs(package_dir: Path, card: dict) -> None:
    outputs_doc = _jload(package_dir / "main_skill" / "output_specs.json") or {}
    domain_doc = _jload(package_dir / "main_skill" / "domain_knowledge.json") or {}
    outputs = outputs_doc.get("outputs", []) if isinstance(outputs_doc, dict) else []
    tables = domain_doc.get("tables", []) if isinstance(domain_doc, dict) else []
    required = list(card.get("required_tables") or [])
    table_brief = []
    for table in tables:
        table_name = table.get("table_name", "")
        columns = table.get("columns", []) or []
        important = [
            c.get("name") for c in columns
            if c.get("semantic_role") in {"PK", "FK", "TIME", "METRIC", "NL_TEXT", "CATEGORY"}
        ][:16]
        table_brief.append({
            "name": table_name,
            "role": table.get("role", "input"),
            "required": table_name in required,
            "columns_count": len(columns),
            "important_columns": important,
        })

    capability = {
        "scenario_name": card.get("display_name", ""),
        "skill_name": card.get("skill_name", ""),
        "namespace": card.get("namespace", ""),
        "summary": card.get("summary", ""),
        "when_to_use": card.get("when_to_use", []),
        "not_for": card.get("not_for", []),
        "required_business_data": {
            "required_tables": required,
            "knowledge_table": card.get("knowledge_table", ""),
            "tables": table_brief,
            "docker_data_mount": "/data",
            "env_data_dir": "BFE_DATA_DIR",
        },
        "outputs": [
            {
                "output_id": o.get("output_id"),
                "name": o.get("name"),
                "format": o.get("fmt", "csv"),
                "status": o.get("status", ""),
                "capability": o.get("capability", ""),
            }
            for o in outputs
        ],
        "tools": [
            {
                "name": t.get("name"),
                "action": t.get("action"),
                "description": t.get("description", ""),
            }
            for t in card.get("tools", [])
        ],
        "recommended_workflow": [
            "Call describe_capability first after installation.",
            "Call describe_schema before writing SQL or matching fields.",
            "Call list_outputs before execute.",
            "Use list_knowledge/search_knowledge for rule or knowledge driven tasks.",
            "Use query_data for ad hoc checks and execute for packaged outputs.",
        ],
    }
    (package_dir / "CAPABILITY.json").write_text(
        json.dumps(capability, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    tool_lines = "\n".join(
        f"- `{t.get('name')}`: {t.get('description', '')}" for t in card.get("tools", [])
    ) or "- 无"
    output_lines = "\n".join(
        f"- `{o.get('output_id')}`: {o.get('name', '')}" for o in outputs
    ) or "- 无"
    table_lines = "\n".join(
        f"- `{t['name']}`: role={t['role']}, required={t['required']}, columns={t['columns_count']}"
        for t in table_brief
    ) or "- 无"
    md = f"""# {capability['scenario_name']} 能力说明

{capability['summary']}

## 适用场景
{chr(10).join('- ' + item for item in capability['when_to_use']) or '- 无'}

## 不适用场景
{chr(10).join('- ' + item for item in capability['not_for']) or '- 无'}

## 需要的业务数据
必需表：{('、'.join(required)) or '无特定要求'}

{table_lines}

Docker 运行时默认从 `/data` 读取业务数据，也可通过 `BFE_DATA_DIR` 指定。

## 可产出结果
{output_lines}

## MCP 工具
{tool_lines}

## 推荐调用流程
1. 先调用 `describe_capability`，确认业务场景、数据要求和工具边界。
2. 再调用 `describe_schema`，读取表结构、字段语义和关联关系。
3. 用 `list_outputs` 查看可执行产出。
4. 对知识/规则驱动任务，先用 `list_knowledge` 或 `search_knowledge` 定位规则条目。
5. 用 `query_data` 做现场查询，用 `execute` 执行封装产出。
"""
    (package_dir / "CAPABILITY.md").write_text(md, encoding="utf-8")


def _contains_platform_path(package_dir: Path) -> bool:
    patterns = (
        "E:\\",
        "business-flow-engine\\data\\scenarios",
        "app.mcp_server",
        "app.runtime.mcp_server",
    )
    for file in package_dir.rglob("*"):
        if not file.is_file() or file.suffix.lower() in {".xlsx", ".xls", ".csv", ".jsonl"}:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except Exception:
            continue
        if any(p in text for p in patterns):
            return True
    return False


def _ensure_release_tool_schema(card: dict) -> None:
    data_dir_prop = {
        "type": "string",
        "description": "可选：新业务数据目录；不传时使用 BFE_DATA_DIR、包内 data/ 或宿主挂载的 /data。",
    }
    out_dir_prop = {
        "type": "string",
        "description": "可选：结果输出目录；不传时使用 BFE_OUT_DIR 或包同级 outputs/。",
    }
    tools = card.setdefault("tools", [])
    existing_actions = {tool.get("action") for tool in tools if isinstance(tool, dict)}
    namespace = card.get("namespace", "scn")
    display = card.get("display_name") or card.get("skill_name") or "业务场景"

    def prepend_tool(action: str, description: str) -> None:
        if action in existing_actions:
            return
        tools.insert(0, {
            "name": f"{namespace}__{action}",
            "action": action,
            "description": description,
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        })
        existing_actions.add(action)

    prepend_tool(
        "list_outputs",
        f"列出「{display}」场景可执行的业务产出、output_id、结果格式和当前可执行状态；调用 execute 前应先查看。",
    )
    prepend_tool(
        "describe_capability",
        f"首次接入或不确定能力用途时先调用：说明「{display}」业务场景是什么、能做什么、需要哪些业务数据、有哪些产出、有哪些工具以及推荐调用流程。",
    )

    for tool in tools:
        schema = tool.setdefault("inputSchema", {"type": "object", "properties": {}, "required": []})
        props = schema.setdefault("properties", {})
        action = tool.get("action", "")
        if action in {"list_knowledge", "search_knowledge", "execute", "query_data"}:
            props.setdefault("data_dir", data_dir_prop)
        if action == "execute":
            props.setdefault("out_dir", out_dir_prop)
        if action == "query_data":
            props.setdefault("save_result", {
                "type": "boolean",
                "description": "是否把查询结果保存为 CSV；默认 false，避免临时查询污染输出目录。",
                "default": False,
            })
            props.setdefault("out_dir", out_dir_prop)


def _write_runtime(package_dir: Path) -> None:
    runtime_dir = package_dir / "bfe_runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "__init__.py").write_text('"""Standalone BFE scenario runtime."""\n', encoding="utf-8")
    mcp_text = (_APP_DIR / "runtime" / "mcp_server.py").read_text(encoding="utf-8")
    mcp_text = mcp_text.replace("python -m app.runtime.mcp_server", "python -m bfe_runtime.mcp_server")
    mcp_text = mcp_text.replace("python -m app.mcp_server", "python -m bfe_runtime.mcp_server")
    mcp_text = mcp_text.replace("`app.runtime.mcp_server`", "`bfe_runtime.mcp_server`")
    mcp_text = mcp_text.replace("`app.mcp_server`", "`bfe_runtime.mcp_server`")
    (runtime_dir / "mcp_server.py").write_text(mcp_text, encoding="utf-8")

    runtime_text = (_APP_DIR / "runtime" / "scenario_runtime.py").read_text(encoding="utf-8")
    (runtime_dir / "scenario_runtime.py").write_text(runtime_text, encoding="utf-8")
    (runtime_dir / "table_io.py").write_text(_STANDALONE_TABLE_IO, encoding="utf-8")
    (package_dir / "run_mcp.py").write_text(_RUN_MCP, encoding="utf-8")


def _write_docker_files(package_dir: Path, skill_name: str) -> None:
    dockerfile = f"""FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \\
    BFE_DATA_DIR=/data \\
    BFE_OUT_DIR=/outputs

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY . /app
RUN mkdir -p /data /outputs

CMD ["python", "-m", "bfe_runtime.mcp_server", "--pkg", "/app"]
"""
    (package_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    package_json = {
        "name": skill_name,
        "version": "1.0.0",
        "private": True,
        "description": "Standalone business scenario MCP package generated by Business Flow Engine.",
        "bin": {skill_name: "bin/mcp-server.mjs"},
        "files": ["SKILL.md", "CAPABILITY.md", "CAPABILITY.json", "agents", "main_skill", "utils", "scripts",
                  "skill_data_reader", "skill_nl_rule_parser", "bfe_runtime", "manifest.json", "mcp.json",
                  "requirements.txt"],
        "scripts": {"mcp": "python -m bfe_runtime.mcp_server --pkg ."},
    }
    (package_dir / "package.json").write_text(json.dumps(package_json, ensure_ascii=False, indent=2), encoding="utf-8")
    bin_dir = package_dir / "bin"
    bin_dir.mkdir(exist_ok=True)
    bin_script = """#!/usr/bin/env node
import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const child = spawn("python", ["-m", "bfe_runtime.mcp_server", "--pkg", root], {
  cwd: root,
  stdio: "inherit",
});
child.on("exit", (code) => process.exit(code ?? 0));
"""
    (bin_dir / "mcp-server.mjs").write_text(bin_script, encoding="utf-8")
    pyproject = f"""[project]
name = "{skill_name}"
version = "1.0.0"
description = "Standalone business scenario MCP package generated by Business Flow Engine."
requires-python = ">=3.10"
dependencies = [
  "pandas>=2.2.0",
  "duckdb>=1.1.0",
  "openpyxl>=3.1.0",
  "python-calamine>=0.7.0",
  "mcp>=1.2.0",
]

[project.scripts]
{skill_name} = "bfe_runtime.mcp_server:main"
"""
    (package_dir / "pyproject.toml").write_text(pyproject, encoding="utf-8")


def _write_install_docs(package_dir: Path, skill_name: str) -> None:
    registry = _docker_registry()
    doc = f"""# {skill_name} 安装说明

这是一个已经脱离蒸馏平台的业务场景能力包。

## ToolPlane / Docker
1. 将本目录构建为 Docker 镜像并推送到 ToolPlane 可访问的镜像仓库。
2. 在 ToolPlane 的 MCP 页面选择 Docker source。
3. 按下面字段填写：
   - Docker Image: `{registry}/{skill_name}:{_DEFAULT_IMAGE_TAG}`
   - Start Command: `python -m bfe_runtime.mcp_server --pkg /app`
   - Server Name: `{skill_name}`

```bash
docker build -t {skill_name}:{_DEFAULT_IMAGE_TAG} .
docker tag {skill_name}:{_DEFAULT_IMAGE_TAG} {registry}/{skill_name}:{_DEFAULT_IMAGE_TAG}
docker push {registry}/{skill_name}:{_DEFAULT_IMAGE_TAG}
```

本地自测：

```bash
docker run -i --rm -v /path/to/business-data:/data {skill_name}:{_DEFAULT_IMAGE_TAG}
```

## 通用 stdio MCP

```json
{{
  "mcpServers": {{
    "{skill_name}": {{
      "command": "python",
      "args": ["/path/to/{skill_name}/run_mcp.py"]
    }}
  }}
}}
```

## Skill 目录
将整个 `{skill_name}` 目录复制到宿主的 skills 目录，入口文件是 `SKILL.md`。

## 新业务数据
把新业务数据文件放到 `/data`、包内 `data/`、或调用工具时传入 `data_dir`。文件名不含后缀应与场景表名一致。
"""
    (package_dir / "TOOLPLANE_INSTALL.md").write_text(doc, encoding="utf-8")


def _write_release_mcp_config(package_dir: Path, skill_name: str) -> None:
    cfg = {
        "mcpServers": {
            skill_name: {
                "command": "python",
                "args": [f"/path/to/{skill_name}/run_mcp.py"],
            }
        }
    }
    (package_dir / "mcp_config.stdio.example.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (package_dir / "mcp_config.example.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _validate_release_package(package_dir: Path) -> list[str]:
    warnings: list[str] = []
    required = [
        "SKILL.md",
        "agents/openai.yaml",
        "manifest.json",
        "mcp.json",
        "requirements.txt",
        "main_skill/scripts/skill_executor.py",
        "bfe_runtime/mcp_server.py",
        "bfe_runtime/scenario_runtime.py",
        "Dockerfile",
    ]
    for rel in required:
        if not (package_dir / rel).exists():
            warnings.append(f"缺少发布文件：{rel}")
    card = _jload(package_dir / "mcp.json") or {}
    if not card.get("tools"):
        warnings.append("mcp.json 未声明工具，第三方无法发现业务能力。")
    if _contains_platform_path(package_dir):
        warnings.append("仍检测到平台私有路径或 app.*.mcp_server 引用。")
    return warnings


def _zip_dir(package_dir: Path, zip_path: Path, root_name: str, exclude_runtime: bool) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(package_dir.rglob("*")):
            if not file.is_file():
                continue
            rel = file.relative_to(package_dir)
            if exclude_runtime and rel.parts and rel.parts[0] in {
                "bfe_runtime", "bin", "run_mcp.py", "Dockerfile", "package.json", "pyproject.toml",
                "TOOLPLANE_INSTALL.md",
            }:
                continue
            if any(part in _IGNORED_DIRS for part in rel.parts):
                continue
            if any(str(rel).endswith(suf) for suf in _IGNORED_SUFFIXES):
                continue
            zf.write(file, Path(root_name) / rel)


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    if not root.exists():
        return ""
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        rel = file.relative_to(root)
        if any(part in _IGNORED_DIRS for part in rel.parts):
            continue
        if any(str(rel).endswith(suf) for suf in _IGNORED_SUFFIXES):
            continue
        h.update(str(rel).replace(os.sep, "/").encode("utf-8"))
        try:
            h.update(file.read_bytes())
        except Exception:
            pass
    return h.hexdigest()


_STANDALONE_TABLE_IO = r'''"""Minimal standalone table loader for released BFE packages."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

_SUPPORTED = (".csv", ".tsv", ".xlsx", ".xls", ".json")
_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}


def _read_excel_fast(path: Path, skip: int | None):
    try:
        return pd.read_excel(path, skiprows=skip, engine="calamine")
    except Exception:
        return pd.read_excel(path, skiprows=skip)


def _read_raw(path: Path, header_row: int = 0) -> pd.DataFrame:
    skip = header_row or None
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return _read_excel_fast(path, skip)
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in ("data", "records", "rows", "items"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        return pd.json_normalize(data if isinstance(data, list) else [data])
    sep = "\t" if suffix == ".tsv" else ","
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return pd.read_csv(path, skiprows=skip, sep=sep, encoding=enc)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, skiprows=skip, sep=sep)


def load_full_frame_cached(file_path: str) -> pd.DataFrame:
    path = Path(file_path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _read_raw(path)
    key = str(path)
    cached = _CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    df = _read_raw(path)
    _CACHE[key] = (mtime, df)
    return df
'''


_RUN_MCP = r'''"""Run the MCP server for this released business scenario package."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bfe_runtime.mcp_server import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["--pkg", str(ROOT)]))
'''
