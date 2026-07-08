"""第三方发布包构建器。

蒸馏阶段生成的 ``data/scenarios/<id>/skills`` 是平台内部工作产物；第三方需要的
是可复制、可下载、符合主流 Agent Skill 目录约定、无本机绝对路径的发布包。本模块把内部技能目录
转换为稳定的 release 包，并让 Agent 平台也从 release 包加载，确保“验证通过”和
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
_BUILDER_VERSION = "3.0"
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
    release_root: Path
    skill_dir: Path
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
            "mcp_zip": f"{prefix}/mcp.zip" if prefix else str(self.docker_zip),
        }
        registry = _docker_registry()
        docker_image = f"{registry}/{self.skill_name}:{_DEFAULT_IMAGE_TAG}"
        start_command = "python -m bfe_runtime.mcp_server --pkg /app"
        return {
            "scenario_id": self.scenario_id,
            "skill_name": self.skill_name,
            "release_root": str(self.release_root),
            "package_dir": str(self.package_dir),
            "mcp_dir": str(self.package_dir),
            "skill_dir": str(self.skill_dir),
            "artifacts_dir": str(self.artifacts_dir),
            "downloads": downloads,
            "install_modes": {
                "skill_directory": {
                    "title": "标准 Skill 目录/zip",
                    "entry": "system_prompt.md",
                    "artifact": "skill.zip",
                    "system_prompt": "system_prompt.md",
                    "recommended": True,
                    "note": "Skill-only 发布物。只包含 system_prompt.md 和标准 Skill 子目录，不混入 MCP/manifest/Docker 文件。",
                },
                "toolplane_docker": {
                    "title": "MCP / Docker 发布包",
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
                    "recommended": False,
                    "note": "MCP-only 发布物。包含 MCP runtime、requirements、Dockerfile 和工具描述，不要求宿主识别 Skill。",
                },
                "mcp_stdio": {
                    "title": "stdio MCP",
                    "command": "python",
                    "args": [str(self.package_dir / "run_mcp.py")],
                    "recommended": False,
                    "note": "适用于支持 command/args 的本地 MCP 宿主。",
                },
            },
            "warnings": self.warnings,
        }


def ensure_release_package(scenario_id: str, base_url: str = "") -> ReleaseBuild:
    """确保 release 包存在并且比内部 skills 新。

    为了避免执行阶段读取内部目录，这个函数会在 Agent 平台 catalog/mount 时自动构建。
    """
    src = Path(store.skills_dir(scenario_id))
    release_base = Path(store.release_dir(scenario_id))
    skill_name = _skill_name(src, scenario_id)
    release_root = release_base / skill_name
    skill_dir = release_root / "skill"
    package_dir = release_root / "mcp"
    manifest_path = release_base / "release.json"
    source_hash = _tree_hash(src)

    if manifest_path.exists() and skill_dir.exists() and package_dir.exists():
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
                    release_root=release_root,
                    skill_dir=skill_dir,
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
    if not (src / "mcp.json").exists() or not list(src.glob("*/SKILL.md")):
        raise FileNotFoundError("该场景尚未生成可发布的能力包（缺少 mcp.json 或标准子 Skill）。")

    release_base = Path(store.release_dir(scenario_id))
    skill_name = _skill_name(src, scenario_id)
    release_root = release_base / skill_name
    skill_dir = release_root / "skill"
    package_dir = release_root / "mcp"
    artifacts_dir = release_base / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_release_packages(release_base, keep=release_root)

    if release_root.exists():
        shutil.rmtree(release_root)
    release_root.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    _build_skill_package(src, skill_dir, skill_name)
    _build_mcp_package(src, package_dir, skill_name, warnings)
    _write_runtime(package_dir)
    _write_docker_files(package_dir, skill_name)
    _write_install_docs(package_dir, skill_name)
    _write_release_mcp_config(package_dir, skill_name)
    warnings.extend(_validate_skill_package(skill_dir))
    warnings.extend(_validate_mcp_package(package_dir))

    skill_zip = artifacts_dir / "skill.zip"
    docker_zip = artifacts_dir / "toolplane-docker.zip"
    _zip_dir(skill_dir, skill_zip, root_name="skill")
    _zip_dir(package_dir, docker_zip, root_name="mcp")

    build = ReleaseBuild(
        scenario_id=scenario_id,
        skill_name=skill_name,
        release_root=release_root,
        skill_dir=skill_dir,
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
        "package_format_version": "2.0",
        "builder_version": _BUILDER_VERSION,
        "artifact_files": {
            "skill_zip": str(skill_zip),
            "toolplane_docker_zip": str(docker_zip),
            "mcp_zip": str(docker_zip),
        },
    }
    build.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return build


def release_status(scenario_id: str, base_url: str = "") -> dict[str, Any]:
    build = ensure_release_package(scenario_id, base_url=base_url)
    status = build.as_dict(base_url=base_url)
    status["ready"] = build.skill_zip.exists()
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
    if name in {"mcp", "mcp.zip", "toolplane-docker", "toolplane-docker.zip", "docker", "docker.zip"}:
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


def _package_has_knowledge_table(package_dir: Path) -> bool:
    """Return whether this generated package actually contains a knowledge table."""
    manifest = _jload(package_dir / "manifest.json") or {}
    if isinstance(manifest, dict):
        if "has_knowledge_table" in manifest:
            return bool(manifest.get("has_knowledge_table"))
        if str(manifest.get("knowledge_table", "")).strip():
            return True
    dispatch = _jload(package_dir / "main_skill" / "dispatch_config.json") or {}
    return bool(isinstance(dispatch, dict) and dispatch.get("knowledge_table"))


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
        if (
            (child / "mcp.json").exists()
            or (child / "run_mcp.py").exists()
            or (child / "mcp" / "mcp.json").exists()
            or (child / "skill" / "system_prompt.md").exists()
        ):
            shutil.rmtree(child)


def _build_skill_package(src: Path, skill_dir: Path, skill_name: str) -> None:
    """Build the Skill-only artifact.

    This directory intentionally contains no MCP descriptors, manifests, Docker files,
    requirements, release docs, runtime package, or platform metadata.
    """
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    skill_dir.mkdir(parents=True, exist_ok=True)

    prompt_src = src / "system_prompt.md"
    prompt = prompt_src.read_text(encoding="utf-8") if prompt_src.exists() else ""
    if not prompt.strip():
        prompt = (
            f"# {skill_name} system prompt\n\n"
            "你是一个业务场景子 Agent。只处理本 Skill 包描述的业务场景；"
            "文件上传、预览、下载和权限隔离由宿主平台负责。"
        )
    (skill_dir / "system_prompt.md").write_text(prompt, encoding="utf-8")

    copied = 0
    for child in sorted(src.iterdir()):
        if not child.is_dir():
            continue
        if child.name in {"agents", "bfe_runtime", "utils", "scripts", "skill_runtime_setup"}:
            continue
        if not (child / "SKILL.md").exists():
            continue
        _copy_tree(child, skill_dir / child.name)
        copied += 1
    if copied == 0:
        raise FileNotFoundError("未找到可发布的标准 Skill 子目录（缺少 */SKILL.md）。")


def _build_mcp_package(src: Path, package_dir: Path, skill_name: str, warnings: list[str]) -> None:
    """Build the MCP-only artifact.

    MCP does not need the host to understand Skill. It keeps only runtime resources,
    capability descriptors and deterministic scripts used by bfe_runtime.
    """
    if package_dir.exists():
        shutil.rmtree(package_dir)
    _copy_tree(src, package_dir)
    _normalize_release_package(package_dir, skill_name, warnings)

    tools_dir = package_dir / "tools"
    if _package_has_knowledge_table(package_dir):
        knowledge_tools = tools_dir / "knowledge"
        knowledge_tools.mkdir(parents=True, exist_ok=True)
        for fname in ("list_knowledge.py", "search_knowledge.py"):
            src_script = package_dir / "skill_knowledge_search" / "scripts" / fname
            if src_script.exists():
                shutil.copy2(src_script, knowledge_tools / fname)

    for rel in (
        "SKILL.md",
        "system_prompt.md",
        "SUBAGENT_SYSTEM_PROMPT.md",
        "TOOLKIT.md",
        "CAPABILITY.md",
        "CAPABILITY.json",
        "SCENARIO_CONTEXT.md",
    ):
        p = package_dir / rel
        if p.exists() and p.is_file():
            p.unlink()
    keep_dirs = {"main_skill", "tools"}
    for child in list(package_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name in keep_dirs:
            continue
        if child.name.startswith("step_") or child.name.startswith("skill_") or child.name in {"agents", "utils", "scripts"}:
            shutil.rmtree(child)
    for rel in ("agents", "skill_runtime_setup"):
        p = package_dir / rel
        if p.exists() and p.is_dir():
            shutil.rmtree(p)
    for skill_md in package_dir.rglob("SKILL.md"):
        if skill_md.is_file():
            skill_md.unlink()


def _normalize_release_package(package_dir: Path, skill_name: str, warnings: list[str]) -> None:
    mcp_path = package_dir / "mcp.json"
    card = _jload(mcp_path) or {}
    has_knowledge = _package_has_knowledge_table(package_dir)

    card.pop("server", None)
    card["skill_name"] = skill_name
    card["server_stdio_fallback"] = {
        "transport": "stdio",
        "command": "python",
        "args": ["run_mcp.py"],
        "note": "在发布包根目录运行；不依赖蒸馏平台代码。",
    }
    card.pop("agent_skill", None)
    card["primary_install_mode"] = "mcp_stdio"
    card["mcp_package"] = {
        "name": skill_name,
        "command": "python",
        "args": ["run_mcp.py"],
        "docker_image_hint": f"{_docker_registry()}/{skill_name}:{_DEFAULT_IMAGE_TAG}",
    }
    card["release"] = {
        "format": "bfe-mcp-package",
        "root": ".",
        "runtime": "bfe_runtime",
        "detached_from_platform": True,
        "install_modes": ["mcp_stdio", "toolplane_docker"],
    }
    if not has_knowledge:
        card["tools"] = [
            tool for tool in card.get("tools", [])
            if not isinstance(tool, dict)
            or tool.get("action") not in {"list_knowledge", "search_knowledge"}
        ]
    _ensure_release_tool_schema(card)
    for tool in card.get("tools", []):
        if not isinstance(tool, dict):
            continue
        desc = str(tool.get("description", ""))
        desc = desc.replace("命令行入口：skill_query_data/scripts/query_data.py。", "由 MCP query_data 工具执行。")
        desc = desc.replace("skill_query_data/scripts/query_data.py", "MCP query_data 工具")
        tool["description"] = desc
    mcp_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_path = package_dir / "manifest.json"
    manifest = _jload(manifest_path) or {}
    if isinstance(manifest, dict):
        mcp_meta = manifest.get("mcp")
        if isinstance(mcp_meta, dict):
            mcp_meta.pop("skill_md", None)
        manifest.pop("skills", None)
        entry_points = manifest.setdefault("entry_points", {})
        if isinstance(entry_points, dict):
            if has_knowledge:
                entry_points["search_knowledge"] = "tools/knowledge/search_knowledge.py"
                entry_points["list_knowledge"] = "tools/knowledge/list_knowledge.py"
            else:
                entry_points.pop("search_knowledge", None)
                entry_points.pop("list_knowledge", None)
            entry_points.pop("query_data", None)
            entry_points.pop("data_reader", None)
            entry_points.pop("nl_rule_parser", None)
            entry_points.pop("context_doc", None)
        manifest_tools = manifest.get("tools", [])
        if not isinstance(manifest_tools, list):
            manifest_tools = []
        for tool in manifest_tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            desc = str(fn.get("description", ""))
            desc = desc.replace("命令行入口：skill_query_data/scripts/query_data.py。", "由 MCP query_data 工具执行。")
            desc = desc.replace("skill_query_data/scripts/query_data.py", "MCP query_data 工具")
            fn["description"] = desc
        runtime_resources = [
            "main_skill/scripts/skill_executor.py",
            "bfe_runtime/mcp_server.py",
            "bfe_runtime/scenario_runtime.py",
        ]
        if has_knowledge:
            runtime_resources[1:1] = [
                "tools/knowledge/search_knowledge.py",
                "tools/knowledge/list_knowledge.py",
            ]
        manifest["runtime_resources"] = runtime_resources
        manifest["verify_instructions"] = (
            "将本 MCP 发布包作为独立能力服务加载。它不依赖蒸馏平台，也不要求宿主识别 Skill；"
            "Skill-only 安装请使用单独的 skill.zip。"
        )
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if _contains_platform_path(package_dir):
        warnings.append("发布包中仍检测到平台路径或 app.*.mcp_server 引用，请检查生成内容。")


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
    namespace = card.get("namespace", "scn")
    display = card.get("display_name") or card.get("skill_name") or "业务场景"
    tools = card.setdefault("tools", [])
    existing_actions = {tool.get("action") for tool in tools if isinstance(tool, dict)}

    data_dir_prop = {
        "type": "string",
        "description": "可选：本地脚本执行时的新业务数据目录。第三方平台文件上传/下载由宿主平台处理。",
    }
    out_dir_prop = {
        "type": "string",
        "description": "可选：结果输出目录；不传时使用 BFE_OUT_DIR 或包同级 outputs/。",
    }

    def ensure_tool(action: str, description: str, props: dict | None = None, required: list[str] | None = None) -> None:
        if action in existing_actions:
            return
        tools.append({
            "name": f"{namespace}__{action}",
            "action": action,
            "description": description,
            "inputSchema": {"type": "object", "properties": props or {}, "required": required or []},
        })
        existing_actions.add(action)

    ensure_tool(
        "describe_capability",
        f"首次接入或不确定能力用途时先调用：说明「{display}」业务场景是什么、能做什么、需要哪些业务数据、有哪些产出、有哪些工具以及推荐调用流程。",
    )
    ensure_tool(
        "list_outputs",
        f"列出「{display}」场景可执行的业务产出、output_id、结果格式和当前可执行状态；调用 execute 前应先查看。",
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
RUN pip install --no-cache-dir "mcp>=1.2.0"
COPY . /app
RUN mkdir -p /data /outputs

CMD ["python", "-m", "bfe_runtime.mcp_server", "--pkg", "/app"]
"""
    (package_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    package_json = {
        "name": skill_name,
        "version": "1.0.0",
        "private": True,
        "description": "Standalone MCP server package generated by Zero Singularity Workshop.",
        "bin": {skill_name: "bin/mcp-server.mjs"},
        "files": ["INSTALL.md", "bin", "main_skill", "tools",
                  "bfe_runtime", "manifest.json", "mcp.json",
                  "mcp_config.example.json", "mcp_config.stdio.example.json",
                  "requirements.txt", "run_mcp.py", "Dockerfile"],
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
description = "Standalone MCP server package generated by Zero Singularity Workshop."
requires-python = ">=3.10"
dependencies = [
  "pandas>=2.2.0",
  "duckdb>=1.1.0",
  "openpyxl>=3.1.0",
  "python-calamine>=0.7.0",
]

[project.optional-dependencies]
mcp = ["mcp>=1.2.0"]

[project.scripts]
{skill_name} = "bfe_runtime.mcp_server:main"
"""
    (package_dir / "pyproject.toml").write_text(pyproject, encoding="utf-8")


def _write_install_docs(package_dir: Path, skill_name: str) -> None:
    registry = _docker_registry()
    doc = f"""# {skill_name} MCP 安装说明

这是一个已经脱离蒸馏平台的 MCP 业务场景服务包。

## 内容边界

本包只用于 MCP / Docker 发布，不是 Skill 包。Skill 发布物请使用单独的 `skill.zip`。

MCP 包包含：

- `mcp.json`：工具清单和能力描述。
- `manifest.json`：场景元数据。
- `main_skill/`、`tools/`：MCP runtime 调用的执行资源。
- `bfe_runtime/`：独立 MCP server 运行时。
- `requirements.txt`：Python 依赖。

## stdio MCP

```bash
pip install -r requirements.txt
python run_mcp.py
```

## Docker

```bash
docker build -t {skill_name}:{_DEFAULT_IMAGE_TAG} .
docker tag {skill_name}:{_DEFAULT_IMAGE_TAG} {registry}/{skill_name}:{_DEFAULT_IMAGE_TAG}
docker push {registry}/{skill_name}:{_DEFAULT_IMAGE_TAG}
```

本地自测：

```bash
docker run -i --rm -v /path/to/business-data:/data {skill_name}:{_DEFAULT_IMAGE_TAG}
```

## MCP 配置

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

文件上传、文件预览和结果下载仍由宿主 Agent 平台负责；本 MCP 只提供业务能力工具。
"""
    (package_dir / "INSTALL.md").write_text(doc, encoding="utf-8")
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


def _validate_skill_package(skill_dir: Path) -> list[str]:
    warnings: list[str] = []
    if not (skill_dir / "system_prompt.md").exists():
        warnings.append("Skill 包缺少 system_prompt.md。")
    skill_files = list(skill_dir.glob("*/SKILL.md"))
    if not skill_files:
        warnings.append("Skill 包缺少标准子 Skill（*/SKILL.md）。")
    forbidden = {
        "mcp.json",
        "manifest.json",
        "requirements.txt",
        "Dockerfile",
        "package.json",
        "pyproject.toml",
        "run_mcp.py",
        "INSTALL.md",
        "TOOLKIT.md",
        "SUBAGENT_SYSTEM_PROMPT.md",
        "CAPABILITY.md",
        "CAPABILITY.json",
        "SCENARIO_CONTEXT.md",
    }
    for rel in forbidden:
        if (skill_dir / rel).exists():
            warnings.append(f"Skill 包不应包含：{rel}")
    for rel in ("bfe_runtime", "agents", "skill_runtime_setup"):
        if (skill_dir / rel).exists():
            warnings.append(f"Skill 包不应包含目录：{rel}")
    return warnings


def _validate_mcp_package(package_dir: Path) -> list[str]:
    warnings: list[str] = []
    has_knowledge = _package_has_knowledge_table(package_dir)
    required = [
        "INSTALL.md",
        "manifest.json",
        "mcp.json",
        "requirements.txt",
        "main_skill/scripts/skill_executor.py",
        "bfe_runtime/mcp_server.py",
        "bfe_runtime/scenario_runtime.py",
        "Dockerfile",
    ]
    if has_knowledge:
        required.extend([
            "tools/knowledge/search_knowledge.py",
            "tools/knowledge/list_knowledge.py",
        ])
    for rel in required:
        if not (package_dir / rel).exists():
            warnings.append(f"缺少发布文件：{rel}")
    card = _jload(package_dir / "mcp.json") or {}
    if not card.get("tools"):
        warnings.append("mcp.json 未声明工具，第三方无法发现业务能力。")
    forbidden = ["SKILL.md", "system_prompt.md", "SUBAGENT_SYSTEM_PROMPT.md", "TOOLKIT.md", "skill_runtime_setup"]
    for rel in forbidden:
        if (package_dir / rel).exists():
            warnings.append(f"MCP 包不应包含 Skill-only 文件或目录：{rel}")
    if _contains_platform_path(package_dir):
        warnings.append("仍检测到平台私有路径或 app.*.mcp_server 引用。")
    return warnings


def _zip_dir(package_dir: Path, zip_path: Path, root_name: str) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(package_dir.rglob("*")):
            if not file.is_file():
                continue
            rel = file.relative_to(package_dir)
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
