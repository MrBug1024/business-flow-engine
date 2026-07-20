"""Safe installation and removal of user-provided Studio skills."""

from __future__ import annotations

import io
import ipaddress
import re
import shutil
import socket
import stat
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.studio.capabilities import registry
from app.studio.models import SkillDefinition


MAX_SKILL_FILES = 256
MAX_SKILL_FILE_BYTES = 10 * 1024 * 1024
MAX_SKILL_TOTAL_BYTES = 25 * 1024 * 1024
MAX_SKILL_ARCHIVE_BYTES = 15 * 1024 * 1024
MAX_SKILL_PATH_LENGTH = 512
MAX_REDIRECTS = 5
_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class SkillDownloadError(RuntimeError):
    pass


def install_skill_files(entries: Iterable[tuple[str, bytes]]) -> SkillDefinition:
    files = _normalize_package_files(entries)
    return _install_normalized_files(files, source="folder-upload")


def install_skill_archive(payload: bytes, *, source: str = "zip-upload") -> SkillDefinition:
    if len(payload) > MAX_SKILL_ARCHIVE_BYTES:
        raise ValueError(f"Skill ZIP exceeds the {MAX_SKILL_ARCHIVE_BYTES} byte archive limit.")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = _read_zip_entries(archive)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError("The supplied file is not a valid, readable Skill ZIP.") from exc
    files = _normalize_package_files(entries)
    return _install_normalized_files(files, source=source)


def install_skill_from_url(url: str) -> SkillDefinition:
    try:
        payload = download_https_zip(url)
    except ValueError:
        raise
    except httpx.HTTPError as exc:
        raise SkillDownloadError(f"Unable to download Skill ZIP: {exc}") from exc
    return install_skill_archive(payload, source="url")


def download_https_zip(url: str, *, transport: httpx.BaseTransport | None = None) -> bytes:
    current = url.strip()
    if not current:
        raise ValueError("Skill ZIP URL cannot be empty.")
    timeout = httpx.Timeout(30.0, connect=10.0)
    with httpx.Client(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
        transport=transport,
        headers={"User-Agent": "AI-Business-Studio-Skill-Installer/1.0"},
    ) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            _validate_public_https_url(current)
            with client.stream("GET", current, headers={"Accept": "application/zip, application/octet-stream"}) as response:
                if transport is None:
                    _validate_connected_peer(response)
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise SkillDownloadError("Skill ZIP redirect did not include a Location header.")
                    if redirect_count >= MAX_REDIRECTS:
                        raise SkillDownloadError("Skill ZIP URL exceeded the redirect limit.")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                declared_size = response.headers.get("content-length")
                if declared_size:
                    try:
                        if int(declared_size) > MAX_SKILL_ARCHIVE_BYTES:
                            raise ValueError(
                                f"Skill ZIP exceeds the {MAX_SKILL_ARCHIVE_BYTES} byte archive limit."
                            )
                    except ValueError as exc:
                        if "exceeds" in str(exc):
                            raise
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_SKILL_ARCHIVE_BYTES:
                        raise ValueError(f"Skill ZIP exceeds the {MAX_SKILL_ARCHIVE_BYTES} byte archive limit.")
                    chunks.append(chunk)
                payload = b"".join(chunks)
                if not zipfile.is_zipfile(io.BytesIO(payload)):
                    raise ValueError("The HTTPS resource is not a valid Skill ZIP.")
                return payload
    raise SkillDownloadError("Skill ZIP download did not complete.")


def delete_user_skill(name: str) -> SkillDefinition:
    definition = next((item for item in registry.list_skills() if item.name == name), None)
    if definition is None:
        raise KeyError(name)
    if definition.kind != "user":
        raise PermissionError(f"System Skill '{name}' cannot be deleted.")
    directory = registry.find_skill_directory(name)
    if directory is None:
        raise KeyError(name)
    skill_root = registry.SYSTEM_SKILLS_ROOT.resolve()
    resolved = directory.resolve()
    if resolved.parent != skill_root or directory.is_symlink() or _is_junction(directory):
        raise PermissionError("Refusing to delete a Skill outside the unified Skill store.")
    if not registry.is_studio_managed_skill_directory(directory):
        raise PermissionError(f"Project-bundled Skill '{name}' cannot be deleted.")
    shutil.rmtree(resolved)
    registry.forget_studio_managed_skill(name)
    registry.clear_skill_registry_cache()
    return definition


def _install_normalized_files(files: Mapping[str, bytes], *, source: str) -> SkillDefinition:
    skill_text = _decode_skill_markdown(files["SKILL.md"])
    name = registry._frontmatter_value(skill_text, "name").strip()
    if not _SKILL_NAME_RE.fullmatch(name):
        raise ValueError("SKILL.md frontmatter name must be a lowercase kebab-case name of at most 64 characters.")

    existing = next((item for item in registry.list_skills() if item.name == name), None)
    if existing is not None:
        if existing.kind == "system":
            raise FileExistsError(f"A locked system Skill named '{name}' already exists.")
        raise FileExistsError(f"User Skill '{name}' is already installed.")

    root = registry.SYSTEM_SKILLS_ROOT
    root.mkdir(parents=True, exist_ok=True)
    destination = root / name
    if destination.exists():
        raise FileExistsError(f"User Skill directory '{name}' already exists.")
    staging_root = root / ".studio-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = staging_root / f"{name}-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for relative, content in sorted(files.items(), key=lambda item: item[0] == "SKILL.md"):
            target = staging.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        staging.rename(destination)
        registry.record_studio_managed_skill(name, source=source)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    registry.clear_skill_registry_cache()
    installed = next((item for item in registry.list_skills() if item.name == name and item.kind == "user"), None)
    if installed is None:
        shutil.rmtree(destination, ignore_errors=True)
        registry.clear_skill_registry_cache()
        raise RuntimeError("Installed Skill could not be discovered by the registry.")
    return installed


def _normalize_package_files(entries: Iterable[tuple[str, bytes]]) -> dict[str, bytes]:
    raw_entries = list(entries)
    if not raw_entries:
        raise ValueError("Skill upload must contain files.")
    if len(raw_entries) > MAX_SKILL_FILES:
        raise ValueError(f"Skill upload exceeds the {MAX_SKILL_FILES} file limit.")

    files: dict[str, bytes] = {}
    casefolded: set[str] = set()
    total = 0
    for raw_path, content in raw_entries:
        relative = _safe_relative_path(raw_path)
        if len(content) > MAX_SKILL_FILE_BYTES:
            raise ValueError(f"Skill file '{relative}' exceeds the per-file size limit.")
        total += len(content)
        if total > MAX_SKILL_TOTAL_BYTES:
            raise ValueError(f"Skill upload exceeds the {MAX_SKILL_TOTAL_BYTES} byte total limit.")
        folded = relative.casefold()
        if folded in casefolded:
            raise ValueError(f"Skill upload contains a duplicate path: {relative}")
        casefolded.add(folded)
        files[relative] = content

    files = _strip_single_wrapper_directory(files)
    if "SKILL.md" not in files:
        raise ValueError("Skill folder must contain SKILL.md at its root.")
    return files


def _strip_single_wrapper_directory(files: Mapping[str, bytes]) -> dict[str, bytes]:
    if "SKILL.md" in files:
        return dict(files)
    roots = {path.split("/", 1)[0] for path in files}
    if len(roots) != 1:
        return dict(files)
    root = next(iter(roots))
    prefix = f"{root}/"
    if f"{prefix}SKILL.md" not in files or any(not path.startswith(prefix) for path in files):
        return dict(files)
    return {path[len(prefix) :]: content for path, content in files.items()}


def _read_zip_entries(archive: zipfile.ZipFile) -> list[tuple[str, bytes]]:
    archive_infos = archive.infolist()
    if len(archive_infos) > MAX_SKILL_FILES:
        raise ValueError(f"Skill ZIP exceeds the {MAX_SKILL_FILES} file limit.")
    file_infos = [item for item in archive_infos if not item.is_dir()]
    declared_total = 0
    entries: list[tuple[str, bytes]] = []
    for info in file_infos:
        _safe_relative_path(info.filename)
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise ValueError(f"Skill ZIP cannot contain symbolic links: {info.filename}")
        if info.flag_bits & 0x1:
            raise ValueError(f"Skill ZIP cannot contain encrypted files: {info.filename}")
        if info.file_size > MAX_SKILL_FILE_BYTES:
            raise ValueError(f"Skill file '{info.filename}' exceeds the per-file size limit.")
        declared_total += info.file_size
        if declared_total > MAX_SKILL_TOTAL_BYTES:
            raise ValueError(f"Skill ZIP exceeds the {MAX_SKILL_TOTAL_BYTES} byte total limit.")
        if info.file_size > 1024 * 1024 and info.compress_size and info.file_size / info.compress_size > 200:
            raise ValueError(f"Skill ZIP entry has an unsafe compression ratio: {info.filename}")
        content = archive.read(info)
        if len(content) != info.file_size:
            raise ValueError(f"Skill ZIP entry size changed while reading: {info.filename}")
        entries.append((info.filename, content))
    return entries


def _safe_relative_path(raw_path: str) -> str:
    if not raw_path or "\x00" in raw_path:
        raise ValueError("Skill file path cannot be empty or contain NUL bytes.")
    value = raw_path.replace("\\", "/").rstrip("/")
    if not value or value.startswith("/") or re.match(r"^[a-zA-Z]:", value):
        raise ValueError(f"Skill file path must be relative: {raw_path}")
    if len(value) > MAX_SKILL_PATH_LENGTH:
        raise ValueError(f"Skill file path is too long: {raw_path}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Skill file path contains an unsafe segment: {raw_path}")
    for part in parts:
        if part != part.strip(" .") or any(character in part for character in '<>:"|?*'):
            raise ValueError(f"Skill file path contains unsupported characters: {raw_path}")
        if part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"Skill file path uses a reserved filename: {raw_path}")
    return "/".join(parts)


def _decode_skill_markdown(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SKILL.md must be valid UTF-8 text.") from exc


def _validate_public_https_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("Skill ZIP URL is malformed.") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("Skill ZIP URL must use HTTPS.")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Skill ZIP URL cannot contain credentials, fragments, or an empty host.")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Skill ZIP URL cannot target a local or internal host.")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ValueError("Skill ZIP URL cannot target a private or non-global IP address.")
        return
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError("Skill ZIP host could not be resolved.") from exc
    if not addresses:
        raise ValueError("Skill ZIP host did not resolve to an address.")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as exc:
            raise ValueError("Skill ZIP host resolved to an invalid address.") from exc
        if not ip.is_global:
            raise ValueError("Skill ZIP host resolves to a private or non-global IP address.")


def _validate_connected_peer(response: httpx.Response) -> None:
    network_stream = response.extensions.get("network_stream")
    if network_stream is None or not hasattr(network_stream, "get_extra_info"):
        raise SkillDownloadError("Unable to verify the Skill ZIP server network address.")
    server_address = network_stream.get_extra_info("server_addr")
    if not server_address:
        raise SkillDownloadError("Unable to verify the Skill ZIP server network address.")
    raw_address = server_address[0] if isinstance(server_address, tuple) else server_address
    try:
        peer = ipaddress.ip_address(str(raw_address).split("%", 1)[0])
    except ValueError as exc:
        raise SkillDownloadError("Skill ZIP server returned an invalid network address.") from exc
    if not peer.is_global:
        raise ValueError("Skill ZIP connection reached a private or non-global IP address.")


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except OSError:
        return True
