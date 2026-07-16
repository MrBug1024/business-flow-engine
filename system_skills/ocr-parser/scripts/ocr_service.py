"""
ocr_service.py — OCR 服务客户端

默认连接配置随 Skill 打包；场景仅在明确声明时覆盖。

支持四种输入方式（优先级从高到低）：
    1. file_content_bytes  原始字节流
    2. file_content_b64    Base64 编码字符串
    3. file_path           本地文件路径
    4. file_url            远程 HTTP/HTTPS 地址
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


_SKILL_ROOT = Path(__file__).resolve().parents[1]
_DEFAULTS_FILE = _SKILL_ROOT / "config" / "defaults.json"
_SCENARIO_BINDING_FILE = _SKILL_ROOT / "references" / "scenario_binding.json"


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _configuration() -> dict[str, str]:
    defaults = _read_object(_DEFAULTS_FILE)
    binding = _read_object(_SCENARIO_BINDING_FILE)
    overrides = binding.get("runtime_overrides") or {}
    if not isinstance(overrides, dict):
        overrides = {}
    keys = {
        str(key)
        for key in (*defaults.keys(), *overrides.keys())
        if isinstance(key, str) and key.startswith("OCR_")
    }
    return {
        key: str(os.environ.get(key, overrides.get(key, defaults.get(key, ""))) or "").strip()
        for key in keys
    }


def _cfg(key: str) -> str:
    """Read sandbox secrets, explicit scenario overrides, then public defaults."""
    return _configuration().get(key, "")


def _result(
    status: str,
    message: str,
    *,
    data: dict[str, Any] | None = None,
    sources: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "data": data or {},
        "sources": sources or [],
        **extra,
    }


def _to_bool(val: str) -> bool:
    return val.strip().lower() in {"1", "true", "yes"}


# ──────────────────────────────────────────────
# 内部数据载体
# ──────────────────────────────────────────────
@dataclass
class _Payload:
    filename: str
    content: bytes
    mime_type: str


# ──────────────────────────────────────────────
# 图片扩展名集合
# ──────────────────────────────────────────────
_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp"}


def _is_image(name: str) -> bool:
    return Path(name).suffix.lstrip(".").lower() in _IMAGE_EXTS


# ──────────────────────────────────────────────
# OCR 服务客户端
# ──────────────────────────────────────────────
class OCRService:
    """线程安全的 OCR 服务客户端（适合全局单例使用）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._client: httpx.Client | None = None

        # 超时配置
        try:
            timeout_sec = max(1.0, min(float(_cfg("OCR_TIMEOUT_SECONDS") or 600), 1800.0))
        except ValueError:
            timeout_sec = 600.0
        self._timeout = httpx.Timeout(timeout_sec, connect=60.0)

    # ── httpx 客户端懒加载 ────────────────────
    def _get_client(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                api_key = _cfg("OCR_API_KEY")
                self._client = httpx.Client(
                    timeout=self._timeout,
                    verify=_to_bool(_cfg("OCR_VERIFY_SSL")),
                    headers={"Authorization": api_key} if api_key else None,
                    trust_env=True,
                )
            return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    # ── 公开解析入口 ──────────────────────────
    def parse(
        self,
        *,
        file_url: str = "",
        file_path: str | Path = "",
        file_content_b64: str = "",
        file_content_bytes: bytes = b"",
        file_name: str = "",
        content_type: str = "",
    ) -> dict[str, Any]:
        """
        解析文档，返回结构化结果。

        Returns:
            成功: {"status": "success", "text": "<提取文本>"}
            失败: {"status": "error",   "message": "<错误描述>"}
        """
        missing = [
            key
            for key in ("OCR_BASE_URL", "OCR_API_KEY")
            if not _cfg(key).strip()
        ]
        if missing:
            return _result(
                "configuration_required",
                "OCR Skill 缺少默认配置或场景覆盖：" + ", ".join(missing),
                missing=missing,
            )
        try:
            payload = self._build_payload(
                file_url=file_url,
                file_path=Path(file_path) if file_path else None,
                file_content_b64=file_content_b64,
                file_content_bytes=file_content_bytes,
                file_name=file_name,
                content_type=content_type,
            )
        except (ValueError, FileNotFoundError) as exc:
            return _result("error", str(exc))
        except httpx.RequestError:
            return _result("provider_unavailable", "输入文件地址当前不可达。")

        image_mode = _is_image(payload.filename)
        return self._do_post(payload, image_mode=image_mode)

    # ── 构建请求载体 ──────────────────────────
    def _build_payload(
        self,
        file_url: str,
        file_path: Path | None,
        file_content_b64: str,
        file_content_bytes: bytes,
        file_name: str,
        content_type: str,
    ) -> _Payload:

        # 1. 原始字节（最高优先级）
        if file_content_bytes:
            name = file_name or "file.bin"
            mime = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
            return _Payload(filename=name, content=file_content_bytes, mime_type=mime)

        # 2. Base64 字符串
        if file_content_b64.strip():
            name = file_name or "file.bin"
            mime = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
            return _Payload(
                filename=name,
                content=base64.b64decode(file_content_b64),
                mime_type=mime,
            )

        # 3. 本地文件路径
        if file_path:
            if not file_path.exists():
                raise FileNotFoundError(f"文件不存在: {file_path}")
            name = file_name or file_path.name
            mime = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
            return _Payload(filename=name, content=file_path.read_bytes(), mime_type=mime)

        # 4. 远程 URL
        if file_url:
            # Never send the OCR service credential to an arbitrary source URL.
            with httpx.Client(
                timeout=self._timeout,
                verify=_to_bool(_cfg("OCR_VERIFY_SSL")),
                follow_redirects=True,
            ) as source_client:
                resp = source_client.get(file_url)
                resp.raise_for_status()
            name = file_name or file_url.split("?")[0].split("/")[-1] or "file"
            mime = content_type or resp.headers.get("Content-Type", "")
            if "." not in name:
                ext = mimetypes.guess_extension(mime) or ".bin"
                name += ext
            if not mime:
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return _Payload(filename=name, content=resp.content, mime_type=mime)

        raise ValueError("必须提供 file_url / file_path / file_content_b64 / file_content_bytes 之一")

    # ── HTTP POST 到 OCR 服务 ─────────────────
    def _do_post(self, payload: _Payload, image_mode: bool) -> dict[str, Any]:
        missing = [
            key
            for key in ("OCR_BASE_URL", "OCR_API_KEY")
            if not _cfg(key).strip()
        ]
        if missing:
            return _result(
                "configuration_required",
                "OCR Skill 缺少默认配置或场景覆盖：" + ", ".join(missing),
                missing=missing,
            )
        base_url = _cfg("OCR_BASE_URL").rstrip("/")

        table  = _to_bool(_cfg("OCR_TABLE_ENABLE_IMAGE" if image_mode else "OCR_TABLE_ENABLE_PDF"))
        rotate = _to_bool(_cfg("OCR_AUTO_ROTATE_IMAGE"  if image_mode else "OCR_AUTO_ROTATE_PDF"))

        try:
            resp = self._get_client().post(
                f"{base_url}/api/parse/sync",
                files={"file": (payload.filename, payload.content, payload.mime_type)},
                data={
                    "backend":      _cfg("OCR_BACKEND"),
                    "lang_list":    _cfg("OCR_LANG_LIST"),
                    "table_enable": str(table).lower(),
                    "auto_rotate":  str(rotate).lower(),
                },
            )
            if resp.status_code in {401, 403}:
                return _result("auth_failed", "OCR 服务凭据无效或权限不足。")
            if resp.status_code == 429 or resp.status_code >= 500:
                return _result("provider_unavailable", "OCR 服务暂时不可用，请稍后重试。")
            if resp.status_code >= 400:
                return _result("error", f"OCR 请求失败（HTTP {resp.status_code}）。")
            try:
                data = resp.json()
            except (ValueError, json.JSONDecodeError):
                return _result("error", "OCR 服务返回了无法解析的响应。")

            if not isinstance(data, dict) or data.get("status") == "failed":
                return _result("error", "OCR 服务未能完成解析。")

            text = str(data.get("markdown") or data.get("text") or "")
            if not text.strip():
                return _result(
                    "empty_content",
                    "OCR 服务完成请求，但没有返回可提取的文本。",
                    sources=[{"filename": payload.filename}],
                )
            return _result(
                "success",
                "OCR 解析完成。",
                data={"text": text},
                sources=[{"filename": payload.filename}],
                text=text,
            )

        except httpx.RequestError:
            return _result("provider_unavailable", "OCR 服务当前不可达，请稍后重试。")
        except Exception:
            return _result("error", "OCR 请求执行失败。")


# ──────────────────────────────────────────────
# 全局单例
# ──────────────────────────────────────────────
_INSTANCE: OCRService | None = None
_INSTANCE_LOCK = threading.RLock()


def get_ocr_service() -> OCRService:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = OCRService()
        return _INSTANCE


def close_ocr_service() -> None:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is not None:
            _INSTANCE.close()
            _INSTANCE = None
