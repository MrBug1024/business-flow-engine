"""Agent 平台独立资源管理：用户上传 Skill、配置 MCP。

这些资源属于第三方 Agent 平台本身，不来自蒸馏场景，也不依赖 release 包。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langchain_openai import ChatOpenAI
from fastapi import UploadFile

from app.core.config import settings
from app.core.llm import get_llm
from app.playground.sandbox import sandbox_execution_env, venv_python
from app.runtime import scenario_runtime as rt


def _safe(uid: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", uid or "anon")


def _root(user_id: str) -> Path:
    d = settings.data_path / "playground" / _safe(user_id) / "resources"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _skills_root(user_id: str) -> Path:
    d = _root(user_id) / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _mcps_file(user_id: str) -> Path:
    return _root(user_id) / "mcps.json"


def _llms_file(user_id: str) -> Path:
    return _root(user_id) / "llms.json"


def _skills_file(user_id: str) -> Path:
    return _root(user_id) / "skills.json"


def _sandboxes_file(user_id: str) -> Path:
    return _root(user_id) / "sandboxes.json"


def _sandboxes_root(user_id: str) -> Path:
    d = _root(user_id) / "sandboxes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _runs_root(user_id: str) -> Path:
    d = _root(user_id) / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean_name(value: str, fallback: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    return (text or fallback)[:80]


def _standard_skill_name(value: str, fallback: str) -> str:
    raw = (value or "").strip()
    if raw and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", raw):
        return raw[:64]
    base = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if not base:
        base = fallback
    base = re.sub(r"-+", "-", base)[:64].strip("-")
    return base or fallback


def _read_text(path: Path, limit: int = 300_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except Exception:
        return ""


def _parse_requirements(text: str) -> list[str]:
    rows: list[str] = []
    for line in text.splitlines():
        item = line.strip()
        if not item or item.startswith("#") or item.startswith(("-r ", "--requirement", "-c ", "--constraint")):
            continue
        if item not in rows:
            rows.append(item)
    return rows


def _append_python_dep(rows: list[str], dep: str) -> None:
    dep = str(dep or "").strip()
    if dep and dep not in rows:
        rows.append(dep)


def _merge_dependency_dict(target: dict, source: dict | None) -> None:
    if not isinstance(source, dict):
        return
    for dep in source.get("python") or []:
        _append_python_dep(target["python"], str(dep))
    node = source.get("node") or {}
    if isinstance(node, dict):
        for name, version in node.items():
            name = str(name or "").strip()
            if name:
                target["node"][name] = str(version or "*")


_PY_IMPORT_TO_DEP = {
    "bs4": "beautifulsoup4>=4.12.0",
    "cv2": "opencv-python>=4.9.0",
    "docx": "python-docx>=1.1.0",
    "duckdb": "duckdb>=1.1.0",
    "fitz": "PyMuPDF>=1.24.0",
    "lxml": "lxml>=5.0.0",
    "markdown": "markdown>=3.6",
    "matplotlib": "matplotlib>=3.8.0",
    "numpy": "numpy>=1.26.0",
    "openpyxl": "openpyxl>=3.1.0",
    "pandas": "pandas>=2.2.0",
    "PIL": "Pillow>=10.0.0",
    "pptx": "python-pptx>=0.6.23",
    "pyarrow": "pyarrow>=15.0.0",
    "pydantic": "pydantic>=2.10.0",
    "requests": "requests>=2.32.0",
    "scipy": "scipy>=1.12.0",
    "sklearn": "scikit-learn>=1.4.0",
    "yaml": "PyYAML>=6.0.0",
}
_PY_STDLIB = set(getattr(sys, "stdlib_module_names", set())) | {
    "__future__",
    "argparse",
    "collections",
    "csv",
    "dataclasses",
    "datetime",
    "decimal",
    "functools",
    "glob",
    "hashlib",
    "html",
    "importlib",
    "io",
    "itertools",
    "json",
    "logging",
    "math",
    "os",
    "pathlib",
    "pickle",
    "random",
    "re",
    "shutil",
    "sqlite3",
    "statistics",
    "subprocess",
    "sys",
    "tempfile",
    "textwrap",
    "time",
    "typing",
    "uuid",
    "zipfile",
}


def _infer_python_dependencies_from_scripts(root: Path) -> dict:
    python_deps: list[str] = []
    inferred_from: list[str] = []
    for script in sorted(root.rglob("*.py")):
        rel = str(script.relative_to(root)).replace("\\", "/")
        if any(part in {"__pycache__", ".venv", "venv", "node_modules"} for part in script.relative_to(root).parts):
            continue
        text = _read_text(script, 300_000)
        imports = set(re.findall(r"(?m)^\s*import\s+([A-Za-z_][\w]*)", text))
        imports.update(re.findall(r"(?m)^\s*from\s+([A-Za-z_][\w]*)\s+import\b", text))
        for name in sorted(imports):
            if name in _PY_STDLIB:
                continue
            dep = _PY_IMPORT_TO_DEP.get(name)
            if dep:
                _append_python_dep(python_deps, dep)
                source = f"{dep} <- {rel}"
                if source not in inferred_from:
                    inferred_from.append(source)
        if "engine=\"calamine\"" in text or "engine='calamine'" in text:
            _append_python_dep(python_deps, "python-calamine>=0.7.0")
            source = f"python-calamine>=0.7.0 <- {rel}"
            if source not in inferred_from:
                inferred_from.append(source)
        if ".to_markdown(" in text:
            _append_python_dep(python_deps, "tabulate>=0.9.0")
            source = f"tabulate>=0.9.0 <- {rel}"
            if source not in inferred_from:
                inferred_from.append(source)
    return {"python": python_deps, "files": inferred_from}


def _parse_dependency_manifest(root: Path, label_root: Path | None = None) -> dict:
    python_deps: list[str] = []
    node_deps: dict[str, str] = {}
    files: list[str] = []
    label_root = label_root or root

    def add_file(path: Path) -> None:
        try:
            files.append(str(path.relative_to(label_root)).replace("\\", "/"))
        except ValueError:
            files.append(str(path))

    req = root / "requirements.txt"
    if req.exists():
        python_deps.extend(_parse_requirements(_read_text(req)))
        add_file(req)

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(_read_text(pyproject))
            for dep in data.get("project", {}).get("dependencies", []) or []:
                _append_python_dep(python_deps, str(dep))
            add_file(pyproject)
        except Exception:
            pass

    package_json = root / "package.json"
    if package_json.exists():
        try:
            pkg = json.loads(_read_text(package_json))
            for group in ("dependencies", "devDependencies"):
                deps = pkg.get(group) if isinstance(pkg, dict) else {}
                if isinstance(deps, dict):
                    for name, version in deps.items():
                        node_deps[str(name)] = str(version)
            add_file(package_json)
        except Exception:
            pass

    return {"python": python_deps, "node": node_deps, "files": files}


def _parse_skill_dependencies(skill_dir: Path) -> dict:
    deps = _parse_dependency_manifest(skill_dir)
    inferred = _infer_python_dependencies_from_scripts(skill_dir)
    for dep in inferred["python"]:
        _append_python_dep(deps["python"], dep)
    for file in inferred["files"]:
        if file not in deps["files"]:
            deps["files"].append(file)
    compatibility = ""

    md = skill_dir / "SKILL.md"
    text = _read_text(md, 20_000)
    if text.startswith("---"):
        end = text.find("\n---", 3)
        front = text[3:end] if end > 0 else ""
        match = re.search(r"(?m)^compatibility:\s*(.+)$", front)
        if match:
            compatibility = match.group(1).strip().strip("'\"")[:500]

    return {
        "python": deps["python"],
        "node": deps["node"],
        "files": deps["files"],
        "compatibility": compatibility,
    }


def _parse_mcp_dependencies(connections: dict[str, dict]) -> dict:
    deps = {"python": [], "node": {}, "files": []}
    roots: list[Path] = []

    def maybe_add_root(raw: Any) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        path = Path(text)
        if path.exists():
            root = path if path.is_dir() else path.parent
            resolved = root.resolve()
            if resolved not in roots:
                roots.append(resolved)

    for conn in (connections or {}).values():
        if not isinstance(conn, dict):
            continue
        _merge_dependency_dict(deps, conn.get("dependencies") if isinstance(conn.get("dependencies"), dict) else None)
        maybe_add_root(conn.get("cwd"))
        maybe_add_root(conn.get("command"))
        for arg in conn.get("args") or []:
            maybe_add_root(arg)

    for root in roots:
        parsed = _parse_dependency_manifest(root)
        _merge_dependency_dict(deps, parsed)
        for file in parsed.get("files") or []:
            label = f"{root.name}/{file}"
            if label not in deps["files"]:
                deps["files"].append(label)
    return deps


def _fingerprint(values: Any) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _safe_rel_path(raw: str) -> Path:
    raw = (raw or "").replace("\\", "/").strip("/")
    parts = PurePosixPath(raw).parts
    if not parts or any(p in {"", ".", ".."} for p in parts):
        raise ValueError(f"非法文件路径：{raw}")
    if PurePosixPath(raw).is_absolute() or re.match(r"^[A-Za-z]:", raw):
        raise ValueError(f"非法文件路径：{raw}")
    return Path(*parts)


def _find_skill_dir(root: Path) -> Path:
    if (root / "SKILL.md").exists():
        return root
    candidates = [p.parent for p in root.rglob("SKILL.md") if p.is_file()]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError("未找到 SKILL.md。请上传包含 SKILL.md 的 Skill 文件夹或 zip。")
    raise ValueError("上传内容中存在多个 SKILL.md。请一次只上传一个完整 Skill。")


def _parse_skill_meta(skill_dir: Path, fallback_name: str) -> dict:
    md = skill_dir / "SKILL.md"
    text = md.read_text(encoding="utf-8", errors="ignore")[:20000]
    name = ""
    description = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            front = text[3:end]
            m = re.search(r"(?m)^name:\s*(.+)$", front)
            if m:
                name = m.group(1).strip().strip("'\"")
            m = re.search(r"(?m)^description:\s*(.+)$", front)
            if m:
                description = m.group(1).strip().strip("'\"")
    if not name:
        m = re.search(r"(?m)^#\s+(.+)$", text)
        if m:
            name = m.group(1).strip()
    if not description:
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith("---") and not line.startswith("#")
        ]
        description = lines[0][:300] if lines else ""
    standard_name = _standard_skill_name(name, _standard_skill_name(fallback_name, "skill"))
    warnings = []
    if name and standard_name != name:
        warnings.append(
            "SKILL.md frontmatter name does not follow the lowercase hyphen Agent Skills convention; "
            f"runtime directory will use {standard_name!r}."
        )
    deps = _parse_skill_dependencies(skill_dir)
    return {
        "name": _clean_name(name, fallback_name),
        "skill_name": standard_name,
        "description": description[:500],
        "dependencies": deps,
        "warnings": warnings,
    }


def list_skills(user_id: str) -> list[dict]:
    rows = _read_json(_skills_file(user_id), [])
    out = []
    changed = False
    for row in rows:
        path = Path(row.get("path", ""))
        if path.exists() and (path / "SKILL.md").exists():
            if not row.get("skill_name") or "dependencies" not in row:
                meta = _parse_skill_meta(path, str(row.get("name") or row.get("id") or "skill"))
                row = {
                    **row,
                    "skill_name": row.get("skill_name") or meta["skill_name"],
                    "dependencies": row.get("dependencies") or meta["dependencies"],
                    "warnings": row.get("warnings") or meta["warnings"],
                }
                changed = True
            out.append(row)
    if len(out) != len(rows) or changed:
        _write_json(_skills_file(user_id), out)
    return out


async def install_skill_from_files(
    user_id: str,
    files: list[UploadFile],
    paths: list[str] | None = None,
    name: str = "",
) -> dict:
    if not files:
        raise ValueError("请上传 Skill 文件夹或 zip。")

    skill_id = _new_id("skill")
    target = _skills_root(user_id) / skill_id
    staging = target.with_name(f"{skill_id}__uploading")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        if len(files) == 1 and (files[0].filename or "").lower().endswith(".zip"):
            zip_path = staging / "upload.zip"
            zip_path.write_bytes(await files[0].read())
            with zipfile.ZipFile(zip_path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    rel = _safe_rel_path(info.filename)
                    dest = staging / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(info))
            zip_path.unlink(missing_ok=True)
        else:
            paths = paths or [f.filename or f"file_{i}" for i, f in enumerate(files)]
            if len(paths) != len(files):
                raise ValueError("上传文件数量与路径数量不一致。")
            for upload, rel_raw in zip(files, paths):
                rel = _safe_rel_path(rel_raw or upload.filename or "")
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(await upload.read())

        skill_dir = _find_skill_dir(staging)
        if target.exists():
            shutil.rmtree(target)
        if skill_dir == staging:
            staging.rename(target)
        else:
            target.mkdir(parents=True, exist_ok=True)
            for child in skill_dir.iterdir():
                shutil.move(str(child), str(target / child.name))
            shutil.rmtree(staging, ignore_errors=True)

        meta = _parse_skill_meta(target, name or skill_id)
        row = {
            "id": skill_id,
            "name": _clean_name(name, meta["name"]),
            "skill_name": meta["skill_name"],
            "description": meta["description"],
            "dependencies": meta["dependencies"],
            "warnings": meta["warnings"],
            "path": str(target.resolve()),
            "created_at": time.time(),
        }
        rows = [r for r in list_skills(user_id) if r.get("id") != skill_id]
        rows.append(row)
        _write_json(_skills_file(user_id), rows)
        return row
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)
        raise


def delete_skill(user_id: str, skill_id: str) -> None:
    rows = list_skills(user_id)
    row = next((r for r in rows if r.get("id") == skill_id), None)
    if row:
        shutil.rmtree(Path(row.get("path", "")), ignore_errors=True)
    _write_json(_skills_file(user_id), [r for r in rows if r.get("id") != skill_id])


# ---------------------------------------------------------------- Sandboxes
def _sandbox_python() -> str:
    return sys.executable


def default_sandbox_resource() -> dict:
    return {
        "id": "",
        "name": "未选择沙箱",
        "type": "none",
        "status": "ready",
        "builtin": True,
        "error": "未选择沙箱时，Agent 可以读取 Skill，但不能执行 Skill 脚本。",
        "dependencies": {"python": [], "node": {}},
    }


def list_sandboxes(user_id: str) -> list[dict]:
    rows = list(_read_json(_sandboxes_file(user_id), []))
    out = []
    changed = False
    root = _sandboxes_root(user_id).resolve()
    for row in rows:
        if not isinstance(row, dict):
            changed = True
            continue
        sid = re.sub(r"[^0-9A-Za-z_-]", "_", str(row.get("id") or ""))[:80]
        if not sid:
            changed = True
            continue
        sandbox_root = (root / sid).resolve()
        original = dict(row)
        row = {
            **row,
            "id": sid,
            "name": _clean_name(str(row.get("name") or ""), sid),
            "type": "python-venv",
            "path": str(sandbox_root),
            "venv_path": str(sandbox_root / "venv"),
            "python": _sandbox_python(),
            "status": str(row.get("status") or "new"),
            "error": str(row.get("error") or ""),
            "dependencies": row.get("dependencies") or {"python": [], "node": {}},
        }
        if row != original:
            changed = True
        out.append(row)
    if changed:
        _write_json(_sandboxes_file(user_id), out)
    return out


def public_sandbox_resource(row: dict) -> dict:
    out = {
        key: value
        for key, value in dict(row).items()
        if key not in {"path", "venv_path", "python", "install_log"}
    }
    if out.get("id"):
        out["managed"] = True
        out["storage_label"] = "平台托管"
    return out


def public_sandbox_resources(user_id: str) -> list[dict]:
    return [public_sandbox_resource(row) for row in list_sandboxes(user_id)]


def sandbox_by_id(user_id: str, sandbox_id: str | None) -> dict | None:
    if not sandbox_id:
        return None
    return next((row for row in list_sandboxes(user_id) if row.get("id") == sandbox_id), None)


def save_sandbox(user_id: str, payload: dict) -> dict:
    sandbox_id = str(payload.get("id") or _new_id("sandbox"))
    sandbox_id = re.sub(r"[^0-9A-Za-z_-]", "_", sandbox_id)[:80] or _new_id("sandbox")
    now = time.time()
    root = (_sandboxes_root(user_id) / sandbox_id).resolve()
    row = {
        "id": sandbox_id,
        "name": _clean_name(str(payload.get("name") or ""), sandbox_id),
        "type": "python-venv",
        "path": str(root),
        "venv_path": str(root / "venv"),
        "python": _sandbox_python(),
        "status": "new",
        "error": "",
        "dependencies": {"python": [], "node": {}},
        "created_at": now,
        "updated_at": now,
    }
    rows = [r for r in list_sandboxes(user_id) if r.get("id") != sandbox_id]
    rows.append(row)
    _write_json(_sandboxes_file(user_id), rows)
    root.mkdir(parents=True, exist_ok=True)
    return row


def delete_sandbox(user_id: str, sandbox_id: str) -> None:
    rows = list_sandboxes(user_id)
    row = next((r for r in rows if r.get("id") == sandbox_id), None)
    if row:
        target = Path(row.get("path", "")).resolve()
        root = _sandboxes_root(user_id).resolve()
        if target == root or root not in target.parents:
            raise ValueError("拒绝删除非沙箱目录")
        shutil.rmtree(target, ignore_errors=True)
    _write_json(_sandboxes_file(user_id), [r for r in rows if r.get("id") != sandbox_id])


def _update_sandbox_row(user_id: str, sandbox_id: str, patch: dict) -> dict:
    rows = list_sandboxes(user_id)
    updated: dict | None = None
    for row in rows:
        if row.get("id") == sandbox_id:
            row.update(patch)
            row["updated_at"] = time.time()
            updated = row
            break
    if updated is None:
        raise ValueError("沙箱环境不存在")
    _write_json(_sandboxes_file(user_id), rows)
    return updated


def _agent_sandbox_id(agent_cfg: dict | None) -> str:
    return str((agent_cfg or {}).get("sandbox_id") or "")


def sandbox_ids_for_config(cfg: dict | None) -> list[str]:
    ids: list[str] = []

    def add(agent_cfg: dict | None) -> None:
        sid = _agent_sandbox_id(agent_cfg)
        if sid and sid not in ids:
            ids.append(sid)

    if isinstance(cfg, dict):
        main = cfg.get("main_agent") if isinstance(cfg.get("main_agent"), dict) else {}
        add(main)
        enabled_subagents = set(main.get("enabled_subagents") or [])
        for sub in cfg.get("subagents") or []:
            if isinstance(sub, dict) and sub.get("id") in enabled_subagents:
                add(sub)
    return ids


def _collect_sandbox_dependencies(user_id: str, cfg: dict | None, sandbox_id: str) -> dict:
    skills = {row["id"]: row for row in list_skills(user_id)}
    mcps = {row["id"]: row for row in list_mcps(user_id)}
    selected_skill_ids: list[str] = []
    selected_mcp_ids: list[str] = []

    def collect(agent_cfg: dict | None) -> None:
        if _agent_sandbox_id(agent_cfg) != sandbox_id:
            return
        for sid in (agent_cfg or {}).get("enabled_skills") or []:
            sid = str(sid)
            if sid in skills and sid not in selected_skill_ids:
                selected_skill_ids.append(sid)
        for mid in (agent_cfg or {}).get("enabled_mcps") or []:
            mid = str(mid)
            if mid in mcps and mid not in selected_mcp_ids:
                selected_mcp_ids.append(mid)

    if isinstance(cfg, dict):
        main = cfg.get("main_agent") if isinstance(cfg.get("main_agent"), dict) else {}
        collect(main)
        enabled_subagents = set(main.get("enabled_subagents") or [])
        for sub in cfg.get("subagents") or []:
            if isinstance(sub, dict) and sub.get("id") in enabled_subagents:
                collect(sub)
    else:
        selected_skill_ids = list(skills)
        selected_mcp_ids = list(mcps)

    python_deps: list[str] = []
    node_deps: dict[str, str] = {}
    deps_target = {"python": python_deps, "node": node_deps}
    for sid in selected_skill_ids:
        deps = skills[sid].get("dependencies") or {}
        _merge_dependency_dict(deps_target, deps)
    for mid in selected_mcp_ids:
        deps = mcps[mid].get("dependencies") or {}
        _merge_dependency_dict(deps_target, deps)
    return {
        "python": python_deps,
        "node": node_deps,
        "skills": selected_skill_ids,
        "mcps": selected_mcp_ids,
        "fingerprint": _fingerprint({
            "python": python_deps,
            "node": node_deps,
            "skills": selected_skill_ids,
            "mcps": selected_mcp_ids,
        }),
    }


def _run_install_step(
    args: list[str],
    cwd: Path,
    log: list[str],
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> int:
    log.append("$ " + " ".join(args))
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    if proc.stdout:
        log.append(proc.stdout)
    if proc.stderr:
        log.append(proc.stderr)
    log.append(f"exit {proc.returncode}")
    return proc.returncode


def install_sandbox_dependencies(user_id: str, sandbox_id: str, cfg: dict | None = None) -> dict:
    row = sandbox_by_id(user_id, sandbox_id)
    if not row:
        raise ValueError("沙箱环境不存在")

    root = Path(row["path"])
    venv_dir = Path(row["venv_path"])
    node_root = root / "node"
    root.mkdir(parents=True, exist_ok=True)
    deps = _collect_sandbox_dependencies(user_id, cfg, sandbox_id)
    req_path = root / "requirements.generated.txt"
    req_path.write_text("\n".join(deps["python"]) + ("\n" if deps["python"] else ""), encoding="utf-8")

    log: list[str] = []
    _update_sandbox_row(user_id, sandbox_id, {"status": "installing", "error": "", "dependencies": deps})
    try:
        py = Path(str(row.get("python") or sys.executable))
        if not venv_python(venv_dir).exists():
            code = _run_install_step([str(py), "-m", "venv", str(venv_dir)], root, log)
            if code != 0:
                raise RuntimeError("创建 venv 失败")

        if deps["python"]:
            code = _run_install_step(
                [
                    str(venv_python(venv_dir)),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "-r",
                    str(req_path),
                ],
                root,
                log,
                timeout=1800,
            )
            if code != 0:
                raise RuntimeError("安装 Python 依赖失败")
        else:
            log.append("No Python dependencies declared by selected skills.")

        if deps["node"]:
            npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")
            if not npm:
                raise RuntimeError("安装 Node 依赖失败：当前服务器未找到 npm")
            node_root.mkdir(parents=True, exist_ok=True)
            package_name = re.sub(r"[^a-z0-9-]+", "-", sandbox_id.lower()).strip("-") or "sandbox"
            package_json = {
                "private": True,
                "name": f"bfe-sandbox-{package_name}",
                "dependencies": deps["node"],
            }
            (node_root / "package.json").write_text(
                json.dumps(package_json, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            code = _run_install_step([npm, "install", "--prefix", str(node_root)], root, log, timeout=1800)
            if code != 0:
                raise RuntimeError("安装 Node 依赖失败")
        else:
            log.append("No Node dependencies declared by selected skills or MCP servers.")

        patch = {
            "status": "ready",
            "error": "",
            "dependencies": deps,
            "installed_at": time.time(),
            "install_log": "\n".join(log)[-80_000:],
        }
        return _update_sandbox_row(user_id, sandbox_id, patch)
    except Exception as exc:
        patch = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "dependencies": deps,
            "install_log": "\n".join(log)[-80_000:],
        }
        return _update_sandbox_row(user_id, sandbox_id, patch)


def ensure_sandbox_dependencies(user_id: str, sandbox_id: str, cfg: dict | None = None) -> dict:
    row = sandbox_by_id(user_id, sandbox_id)
    if not row:
        raise ValueError("沙箱环境不存在")
    deps = _collect_sandbox_dependencies(user_id, cfg, sandbox_id)
    installed = row.get("dependencies") or {}
    venv_ready = venv_python(Path(row["venv_path"])).exists()
    if (
        row.get("status") == "ready"
        and installed.get("fingerprint") == deps.get("fingerprint")
        and venv_ready
    ):
        return row
    return install_sandbox_dependencies(user_id, sandbox_id, cfg)


def _normalize_mcp_connections(raw: Any, fallback_name: str) -> dict[str, dict]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("MCP 配置必须是 JSON 对象。")

    servers = raw.get("mcpServers") if isinstance(raw.get("mcpServers"), dict) else raw
    if not isinstance(servers, dict) or not servers:
        raise ValueError("MCP 配置必须包含 mcpServers，或直接提供 server 配置对象。")

    normalized: dict[str, dict] = {}
    for key, cfg in servers.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"MCP 服务 {key} 配置必须是对象。")
        name = re.sub(r"[^0-9A-Za-z_-]", "_", str(key or fallback_name))[:60] or fallback_name
        item = dict(cfg)
        if "transport" not in item:
            if item.get("command"):
                item["transport"] = "stdio"
            elif item.get("url"):
                item["transport"] = "sse" if str(item["url"]).rstrip("/").endswith("/sse") else "streamable_http"
            else:
                raise ValueError(f"MCP 服务 {key} 缺少 command 或 url。")
        if item["transport"] == "stdio":
            item["args"] = [str(x) for x in item.get("args") or []]
        normalized[name] = item
    return normalized


def list_mcps(user_id: str) -> list[dict]:
    rows = list(_read_json(_mcps_file(user_id), []))
    out: list[dict] = []
    changed = False
    for row in rows:
        if not isinstance(row, dict):
            changed = True
            continue
        if "dependencies" not in row:
            row = {**row, "dependencies": _parse_mcp_dependencies(row.get("connections") or {})}
            changed = True
        if row.get("status") == "error" and row.get("connections"):
            row = {**row, "status": "configured"}
            changed = True
        out.append(row)
    if changed:
        _write_json(_mcps_file(user_id), out)
    return out


def _mcp_client_class():
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "当前 Python 环境未安装 langchain-mcp-adapters；MCP 连接不可用。"
            "请在运行 run.py 的同一个环境执行：pip install langchain-mcp-adapters"
        ) from exc
    return MultiServerMCPClient


def mcp_adapter_status() -> dict:
    try:
        _mcp_client_class()
        return {"available": True, "error": ""}
    except RuntimeError as exc:
        return {"available": False, "error": str(exc)}


async def test_mcp_connections(connections: dict[str, dict]) -> tuple[list[str], str]:
    try:
        MultiServerMCPClient = _mcp_client_class()
        client = MultiServerMCPClient(connections, tool_name_prefix=True)
        tools = await asyncio.wait_for(client.get_tools(), timeout=12)
        return [getattr(t, "name", "") for t in tools if getattr(t, "name", "")], ""
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"


async def load_mcp_tools(connections: dict[str, dict], timeout: int = 15) -> tuple[list[BaseTool], str]:
    if not connections:
        return [], ""
    try:
        MultiServerMCPClient = _mcp_client_class()
        client = MultiServerMCPClient(connections, tool_name_prefix=True)
        tools = await asyncio.wait_for(client.get_tools(), timeout=timeout)
        return list(tools), ""
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"


async def save_mcp(user_id: str, payload: dict) -> dict:
    mcp_id = str(payload.get("id") or _new_id("mcp"))
    name = _clean_name(str(payload.get("name") or ""), mcp_id)
    raw_config = payload.get("config") or {}
    connections = _normalize_mcp_connections(raw_config, name)
    deps = _parse_mcp_dependencies(connections)
    tools, error = await test_mcp_connections(connections)
    row = {
        "id": mcp_id,
        "name": name,
        "config": raw_config,
        "connections": connections,
        "dependencies": deps,
        "tools": tools,
        "status": "connected" if not error else "configured",
        "error": error,
        "updated_at": __import__("time").time(),
    }
    rows = [r for r in list_mcps(user_id) if r.get("id") != mcp_id]
    rows.append(row)
    _write_json(_mcps_file(user_id), rows)
    return row


def delete_mcp(user_id: str, mcp_id: str) -> None:
    _write_json(_mcps_file(user_id), [r for r in list_mcps(user_id) if r.get("id") != mcp_id])


def list_llms(user_id: str) -> list[dict]:
    return list(_read_json(_llms_file(user_id), []))


def _public_llm(row: dict) -> dict:
    out = dict(row)
    out["api_key_set"] = bool(str(out.get("api_key") or "").strip())
    return out


def save_llm(user_id: str, payload: dict) -> dict:
    llm_id = str(payload.get("id") or _new_id("llm"))
    name = _clean_name(str(payload.get("name") or ""), llm_id)
    model = str(payload.get("model") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    try:
        temperature = float(payload.get("temperature", 0.0))
    except (TypeError, ValueError):
        temperature = 0.0
    if not model:
        raise ValueError("LLM 配置缺少 model。")
    if not base_url:
        raise ValueError("LLM 配置缺少 base_url。")
    row = {
        "id": llm_id,
        "name": name,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "temperature": temperature,
        "updated_at": __import__("time").time(),
    }
    rows = [r for r in list_llms(user_id) if r.get("id") != llm_id]
    rows.append(row)
    _write_json(_llms_file(user_id), rows)
    return _public_llm(row)


def delete_llm(user_id: str, llm_id: str) -> None:
    _write_json(_llms_file(user_id), [r for r in list_llms(user_id) if r.get("id") != llm_id])


def llm_by_id(user_id: str, llm_id: str | None) -> dict | None:
    if not llm_id:
        return None
    return next((r for r in list_llms(user_id) if r.get("id") == llm_id), None)


def create_llm(user_id: str, llm_id: str | None, *, streaming: bool = True):
    row = llm_by_id(user_id, llm_id)
    if not row:
        return get_llm()
    if not str(row.get("api_key") or "").strip():
        return None
    return ChatOpenAI(
        model=str(row.get("model") or ""),
        api_key=str(row.get("api_key") or ""),
        base_url=str(row.get("base_url") or ""),
        temperature=float(row.get("temperature", 0.0)),
        streaming=streaming,
    )


def default_llm_resource() -> dict:
    return {
        "id": "",
        "name": "平台默认 LLM",
        "model": settings.llm_model,
        "base_url": settings.openai_base_url,
        "temperature": settings.llm_temperature,
        "api_key": "",
        "api_key_set": settings.llm_enabled,
        "builtin": True,
    }


def all_resources(user_id: str) -> dict:
    return {
        "skills": list_skills(user_id),
        "mcps": list_mcps(user_id),
        "llms": [_public_llm(row) for row in list_llms(user_id)],
        "default_llm": default_llm_resource(),
        "default_sandbox": default_sandbox_resource(),
        "sandboxes": public_sandbox_resources(user_id),
        "mcp_adapter": mcp_adapter_status(),
    }


def skill_paths(user_id: str, ids: list[str]) -> list[str]:
    allowed = {r["id"]: r for r in list_skills(user_id)}
    return [allowed[i]["path"] for i in ids if i in allowed]


def _skill_source_path(row: dict) -> Path:
    return Path(str(row.get("path") or ""))


def _is_main_scenario_skill(row: dict) -> bool:
    p = _skill_source_path(row)
    return (
        str(row.get("skill_name") or "") == "scenario-main"
        or (
            (p / "scripts" / "skill_executor.py").exists()
            and (p / "domain_knowledge.json").exists()
            and (p / "dispatch_config.json").exists()
        )
    )


def _is_knowledge_search_skill(row: dict) -> bool:
    p = _skill_source_path(row)
    return (
        str(row.get("skill_name") or "") == "knowledge-search"
        or (
            (p / "scripts" / "list_knowledge.py").exists()
            and (p / "scripts" / "search_knowledge.py").exists()
        )
    )


def _safe_tool_namespace(value: str, fallback: str) -> str:
    raw = re.sub(r"[^0-9A-Za-z_]+", "_", str(value or "")).strip("_")
    if raw and re.fullmatch(r"[A-Za-z_][0-9A-Za-z_]{0,63}", raw):
        return raw
    return fallback


def _scenario_tools(namespace: str, display_name: str, output_ids: list[str]) -> list[dict]:
    output_desc = output_ids or ["先调用 list_outputs 获取可选 output_id"]
    return [
        {
            "name": f"{namespace}__describe_capability",
            "action": "describe_capability",
            "description": f"说明「{display_name}」业务场景是什么、需要哪些数据、有哪些工具和推荐流程。",
        },
        {
            "name": f"{namespace}__list_outputs",
            "action": "list_outputs",
            "description": f"列出「{display_name}」场景可执行的业务产出和 output_id。",
        },
        {
            "name": f"{namespace}__describe_schema",
            "action": "describe_schema",
            "description": f"获取「{display_name}」场景的表结构、字段语义、关联键和知识表结构。",
        },
        {
            "name": f"{namespace}__list_knowledge",
            "action": "list_knowledge",
            "description": f"浏览「{display_name}」知识/规则表条目。需要规则驱动判断时先调用。",
        },
        {
            "name": f"{namespace}__search_knowledge",
            "action": "search_knowledge",
            "description": f"按关键词搜索「{display_name}」知识/规则表。需要定位规则原文时调用。",
        },
        {
            "name": f"{namespace}__execute",
            "action": "execute",
            "description": (
                f"执行「{display_name}」打包业务产出。可选 output_id：{output_desc}。"
                "知识驱动场景返回规则行后，应继续用 query_data 落地查询。"
            ),
        },
        {
            "name": f"{namespace}__query_data",
            "action": "query_data",
            "description": (
                f"对「{display_name}」当前业务数据执行 DuckDB SQL。"
                "表名必须用 describe_schema 中列出的原始表名。"
            ),
        },
    ]


def _materialize_scenario_action_package(
    rows: list[dict],
    package_dir: Path,
    namespace_hint: str = "",
) -> rt.ScenarioPackage | None:
    main = next((row for row in rows if _is_main_scenario_skill(row)), None)
    if not main:
        return None

    main_src = _skill_source_path(main)
    if not main_src.exists():
        return None

    package_dir.mkdir(parents=True, exist_ok=True)
    main_dest = package_dir / "main_skill"
    if main_dest.exists():
        shutil.rmtree(main_dest)
    shutil.copytree(main_src, main_dest)

    knowledge = next((row for row in rows if _is_knowledge_search_skill(row)), None)
    if knowledge:
        scripts = _skill_source_path(knowledge) / "scripts"
        tools_dir = package_dir / "tools" / "knowledge"
        if tools_dir.exists():
            shutil.rmtree(tools_dir)
        tools_dir.mkdir(parents=True, exist_ok=True)
        for name in ("list_knowledge.py", "search_knowledge.py"):
            src = scripts / name
            if src.exists():
                shutil.copy2(src, tools_dir / name)

    domain = _read_json(main_dest / "domain_knowledge.json", {})
    dispatch = _read_json(main_dest / "dispatch_config.json", {})
    outputs = (_read_json(main_dest / "output_specs.json", {}) or {}).get("outputs", [])
    display_name = str(domain.get("scenario") or main.get("name") or "业务场景")
    fallback_ns = "s_" + _fingerprint([row.get("id") for row in rows])[:10]
    namespace = _safe_tool_namespace(namespace_hint, fallback_ns)
    output_ids = [str(o.get("output_id") or "") for o in outputs if o.get("output_id")]
    tables = domain.get("tables", []) if isinstance(domain, dict) else []
    required_tables = [
        str(t.get("table_name"))
        for t in tables
        if t.get("table_name") and str(t.get("role") or "") in {"input", "knowledge", "rule"}
    ]
    summary = str(main.get("description") or f"{display_name} 业务能力")
    card = {
        "protocol": "playground-tools",
        "namespace": namespace,
        "display_name": display_name,
        "summary": summary,
        "required_tables": required_tables,
        "knowledge_table": dispatch.get("knowledge_table", ""),
        "execution_mode": "knowledge_engine" if dispatch else "",
        "tools": _scenario_tools(namespace, display_name, output_ids),
    }
    (package_dir / "mcp.json").write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    (package_dir / "manifest.json").write_text(
        json.dumps({"namespace": namespace, "scenario_name": display_name}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pkg = rt.ScenarioPackage.load(package_dir)
    return pkg if pkg.is_ready() else None


def _default_tool_arg(value: str | None, default: Path) -> str:
    text = str(value or "").strip()
    return text or str(default)


def scenario_action_tools(
    user_id: str,
    ids: list[str],
    package_dir: Path,
    data_dir: Path,
    out_dir: Path,
    namespace_hint: str = "",
) -> list[BaseTool]:
    rows_by_id = {row["id"]: row for row in list_skills(user_id)}
    rows = [rows_by_id[i] for i in ids or [] if i in rows_by_id]
    pkg = _materialize_scenario_action_package(rows, package_dir, namespace_hint=namespace_hint)
    if not pkg:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    tools: list[BaseTool] = []
    by_action = {t.get("action"): t for t in pkg.tools if t.get("action") and t.get("name")}

    def desc(action: str, extra: str = "") -> str:
        base = str((by_action.get(action) or {}).get("description") or "")
        suffix = "请直接调用此工具完成业务动作，不要临时创建 Python/SQL 脚本。"
        return " ".join(x for x in [base, extra, suffix] if x).strip()

    if "describe_capability" in by_action:
        def describe_capability() -> str:
            """说明当前业务能力包的用途、边界、所需数据和推荐调用流程。"""
            return rt.call_action(pkg, "describe_capability", {})

        tools.append(StructuredTool.from_function(
            describe_capability,
            name=by_action["describe_capability"]["name"],
            description=desc("describe_capability"),
        ))

    if "list_outputs" in by_action:
        def list_outputs() -> str:
            """列出当前业务能力包可执行的 output_id。"""
            return rt.call_action(pkg, "list_outputs", {})

        tools.append(StructuredTool.from_function(
            list_outputs,
            name=by_action["list_outputs"]["name"],
            description=desc("list_outputs"),
        ))

    if "describe_schema" in by_action:
        def describe_schema() -> str:
            """返回业务表结构、字段语义、关联键和知识表结构。"""
            return rt.call_action(pkg, "describe_schema", {})

        tools.append(StructuredTool.from_function(
            describe_schema,
            name=by_action["describe_schema"]["name"],
            description=desc("describe_schema"),
        ))

    if "list_knowledge" in by_action:
        def list_knowledge(limit: int = 50, data_dir: str = "") -> str:
            """浏览知识/规则表条目。"""
            return rt.call_action(pkg, "list_knowledge", {
                "limit": limit,
                "data_dir": _default_tool_arg(data_dir, data_dir_path),
            })

        data_dir_path = data_dir
        tools.append(StructuredTool.from_function(
            list_knowledge,
            name=by_action["list_knowledge"]["name"],
            description=desc("list_knowledge"),
        ))

    if "search_knowledge" in by_action:
        def search_knowledge(keyword: str = "", limit: int = 20, data_dir: str = "") -> str:
            """按关键词搜索知识/规则表条目。"""
            return rt.call_action(pkg, "search_knowledge", {
                "keyword": keyword,
                "limit": limit,
                "data_dir": _default_tool_arg(data_dir, data_dir_path),
            })

        data_dir_path = data_dir
        tools.append(StructuredTool.from_function(
            search_knowledge,
            name=by_action["search_knowledge"]["name"],
            description=desc("search_knowledge"),
        ))

    if "execute" in by_action:
        def execute(output_id: str, params: Any = "", max_rows: int = 20000, data_dir: str = "", out_dir: str = "") -> str:
            """执行打包业务产出。"""
            return rt.call_action(pkg, "execute", {
                "output_id": output_id,
                "params": params,
                "max_rows": max_rows,
                "data_dir": _default_tool_arg(data_dir, data_dir_path),
                "out_dir": _default_tool_arg(out_dir, out_dir_path),
            })

        data_dir_path = data_dir
        out_dir_path = out_dir
        tools.append(StructuredTool.from_function(
            execute,
            name=by_action["execute"]["name"],
            description=desc("execute"),
        ))

    if "query_data" in by_action:
        def query_data(sql: str, save_result: bool = False, data_dir: str = "", out_dir: str = "") -> str:
            """对当前业务数据执行 DuckDB SQL。"""
            return rt.call_action(pkg, "query_data", {
                "sql": sql,
                "save_result": save_result,
                "data_dir": _default_tool_arg(data_dir, data_dir_path),
                "out_dir": _default_tool_arg(out_dir, out_dir_path),
            })

        data_dir_path = data_dir
        out_dir_path = out_dir
        tools.append(StructuredTool.from_function(
            query_data,
            name=by_action["query_data"]["name"],
            description=desc("query_data"),
        ))

    return tools


def _with_sandbox_env(conn: dict, sandbox: dict | None) -> dict:
    if not sandbox:
        return dict(conn)
    item = dict(conn)
    if item.get("transport") != "stdio":
        return item
    sandbox_env = sandbox_execution_env(sandbox)
    user_env = item.get("env") if isinstance(item.get("env"), dict) else {}
    merged_path = sandbox_env.get("PATH", "")
    if user_env.get("PATH"):
        merged_path = merged_path + os.pathsep + str(user_env.get("PATH"))
    env = {**sandbox_env, **{str(k): str(v) for k, v in user_env.items()}}
    if merged_path:
        env["PATH"] = merged_path
    item["env"] = env
    return item


def mcp_connections(user_id: str, ids: list[str], sandbox: dict | None = None) -> dict[str, dict]:
    allowed = {r["id"]: r for r in list_mcps(user_id)}
    out: dict[str, dict] = {}
    for mcp_id in ids:
        row = allowed.get(mcp_id)
        if not row or row.get("status") not in {"connected", "configured"}:
            continue
        for name, conn in (row.get("connections") or {}).items():
            out[f"{mcp_id}_{name}"] = _with_sandbox_env(conn, sandbox)
    return out


def resource_summary(user_id: str, cfg: dict) -> dict:
    skills = {r["id"]: r for r in list_skills(user_id)}
    mcps = {r["id"]: r for r in list_mcps(user_id)}
    llms = {r["id"]: r for r in list_llms(user_id)}
    sandboxes = {r["id"]: r for r in list_sandboxes(user_id)}
    main = cfg.get("main_agent") or {}
    enabled_subagents = set(main.get("enabled_subagents") or [])

    def llm_row(llm_id: str) -> dict:
        if not llm_id:
            return {"id": "", "name": "平台默认 LLM", "model": settings.llm_model}
        row = llms.get(llm_id)
        if not row:
            return {"id": llm_id, "name": "已删除 LLM", "missing": True}
        return {"id": row.get("id"), "name": row.get("name"), "model": row.get("model")}

    def skill_rows(ids: list[str]) -> list[dict]:
        rows = []
        for skill_id in ids or []:
            row = skills.get(skill_id)
            if row:
                rows.append({
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "description": row.get("description", ""),
                })
        return rows

    def sandbox_row(sandbox_id: str) -> dict:
        if not sandbox_id:
            return default_sandbox_resource()
        row = sandboxes.get(sandbox_id)
        if not row:
            return {"id": sandbox_id, "name": "已删除沙箱", "missing": True}
        return {
            "id": row.get("id"),
            "name": row.get("name"),
            "status": row.get("status"),
            "error": row.get("error", ""),
            "dependencies": row.get("dependencies") or {},
        }

    def mcp_rows(ids: list[str]) -> list[dict]:
        rows = []
        for mcp_id in ids or []:
            row = mcps.get(mcp_id)
            if row:
                rows.append({
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "status": row.get("status"),
                    "tools": row.get("tools", []),
                    "error": row.get("error", ""),
                })
        return rows

    return {
        "main_agent": {
            "name": main.get("name") or "主 Agent",
            "llm": llm_row(str(main.get("llm_id") or "")),
            "sandbox": sandbox_row(str(main.get("sandbox_id") or "")),
            "skills": skill_rows(main.get("enabled_skills") or []),
            "mcps": mcp_rows(main.get("enabled_mcps") or []),
        },
        "subagents": [
            {
                "id": sub.get("id"),
                "name": sub.get("name") or sub.get("id"),
                "llm": llm_row(str(sub.get("llm_id") or "")),
                "sandbox": sandbox_row(str(sub.get("sandbox_id") or "")),
                "skills": skill_rows(sub.get("enabled_skills") or []),
                "mcps": mcp_rows(sub.get("enabled_mcps") or []),
            }
            for sub in cfg.get("subagents") or []
            if isinstance(sub, dict) and sub.get("id") in enabled_subagents
        ],
    }


def _copy_selected_skills(user_id: str, ids: list[str], dest: Path) -> int:
    skills = {r["id"]: r for r in list_skills(user_id)}
    copied = 0
    dest.mkdir(parents=True, exist_ok=True)
    for skill_id in ids or []:
        row = skills.get(skill_id)
        if not row:
            continue
        src = Path(row.get("path", ""))
        if not src.exists() or not (src / "SKILL.md").exists():
            continue
        skill_name = _standard_skill_name(str(row.get("skill_name") or ""), skill_id)
        target = dest / skill_name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src, target)
        copied += 1
    return copied


def _copy_attachments(attachment_dir: Path, dest: Path) -> int:
    copied = 0
    if not attachment_dir.exists():
        return copied
    dest.mkdir(parents=True, exist_ok=True)
    for src in attachment_dir.iterdir():
        if not src.is_file():
            continue
        shutil.copy2(src, dest / src.name)
        copied += 1
    return copied


def _virtual_path(*parts: str) -> str:
    return "/" + "/".join(p.strip("/\\") for p in parts if p.strip("/\\"))


def prepare_runtime_workspace(user_id: str, cfg: dict, attachment_dir: Path) -> dict:
    run_id = _new_id("run")
    root = _runs_root(user_id) / run_id
    root.mkdir(parents=True, exist_ok=True)
    main = cfg.get("main_agent") or {}
    enabled_subagents = set(main.get("enabled_subagents") or [])

    main_dest = root / "skills" / "main"
    main_count = _copy_selected_skills(user_id, main.get("enabled_skills") or [], main_dest)
    sub_sources: dict[str, str] = {}
    for sub in cfg.get("subagents") or []:
        if not isinstance(sub, dict) or sub.get("id") not in enabled_subagents:
            continue
        sub_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(sub.get("id") or "subagent"))[:80]
        dest = root / "skills" / "subagents" / sub_id
        if _copy_selected_skills(user_id, sub.get("enabled_skills") or [], dest):
            sub_sources[str(sub.get("id"))] = _virtual_path("skills", "subagents", sub_id)

    attachment_count = _copy_attachments(attachment_dir, root / "attachments")
    return {
        "root": str(root.resolve()),
        "main_skill_sources": [_virtual_path("skills", "main")] if main_count else [],
        "subagent_skill_sources": sub_sources,
        "attachments_path": _virtual_path("attachments") if attachment_count else "",
        "attachment_count": attachment_count,
    }


def cleanup_runtime_workspace(root: str) -> None:
    if not root:
        return
    target = Path(root)
    runs = target.parent
    if runs.name != "runs" or not target.name.startswith("run_"):
        return
    shutil.rmtree(target, ignore_errors=True)
