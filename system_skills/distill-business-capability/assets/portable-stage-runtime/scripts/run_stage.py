#!/usr/bin/env python3
"""Create and validate bounded work orders for one distilled business stage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


MAX_JSON_BYTES = 4 * 1024 * 1024
RAW_DATA_EXTENSIONS = {
    ".csv", ".tsv", ".xlsx", ".xls", ".xlsb", ".parquet", ".jsonl", ".ndjson",
    ".sqlite", ".sqlite3", ".db", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff",
    ".docx", ".pptx",
}
ARTIFACT_ROOT_ENV = "BUSINESS_ARTIFACT_ROOT"


class StageRuntimeError(ValueError):
    pass


class ArtifactPathError(StageRuntimeError):
    pass


def artifact_root() -> Path:
    configured = str(os.environ.get(ARTIFACT_ROOT_ENV, "")).strip()
    if not configured:
        raise ArtifactPathError(
            "Persistent artifact output is blocked: the host must set BUSINESS_ARTIFACT_ROOT."
        )
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise ArtifactPathError("BUSINESS_ARTIFACT_ROOT must be an absolute host-managed directory.")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ArtifactPathError("BUSINESS_ARTIFACT_ROOT is not a writable artifact directory.")
    return root


def normalize_artifact_name(value: str | Path, label: str = "artifact name") -> str:
    raw = str(value or "").strip()
    normalized = raw.replace("\\", "/")
    if not normalized:
        raise ArtifactPathError(f"{label} must be a non-empty relative artifact name.")
    if (
        "\x00" in normalized
        or normalized.startswith("/")
        or raw.startswith("\\")
        or re.match(r"^[A-Za-z]:", raw)
    ):
        raise ArtifactPathError(f"{label} must be a safe relative artifact name, not an absolute path.")
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise ArtifactPathError(f"{label} must not contain traversal or drive-qualified segments.")
    return PurePosixPath(*parts).as_posix()


def _absolute_artifact_input(raw: str) -> bool:
    return (
        Path(raw).expanduser().is_absolute()
        or raw.replace("\\", "/").startswith("/")
        or raw.startswith("\\")
        or bool(re.match(r"^[A-Za-z]:", raw))
    )


def resolve_artifact_name(value: str | Path, label: str = "artifact name") -> tuple[Path, str]:
    root = artifact_root()
    raw = str(value or "").strip()
    if _absolute_artifact_input(raw):
        target = Path(raw).expanduser().resolve()
        try:
            relative_path = target.relative_to(root).as_posix()
        except ValueError as exc:
            raise ArtifactPathError(
                f"{label} must be a safe relative artifact name or an already-translated path inside BUSINESS_ARTIFACT_ROOT."
            ) from exc
    else:
        relative_path = normalize_artifact_name(raw, label)
        target = (root.joinpath(*PurePosixPath(relative_path).parts)).resolve()
    if target == root or root not in target.parents:
        raise ArtifactPathError(f"{label} escapes BUSINESS_ARTIFACT_ROOT.")
    return target, relative_path


def artifact_relative_path(path: Path) -> tuple[Path, str]:
    root = artifact_root()
    resolved = path.expanduser().resolve()
    try:
        relative_path = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ArtifactPathError("Artifact output must stay inside BUSINESS_ARTIFACT_ROOT.") from exc
    if not relative_path or relative_path == ".":
        raise ArtifactPathError("Artifact output must name a file below BUSINESS_ARTIFACT_ROOT.")
    return resolved, relative_path


def artifact_reference(path: Path, kind: str) -> dict[str, Any]:
    resolved, relative_path = artifact_relative_path(path)
    if not resolved.is_file():
        raise ArtifactPathError("Artifact file does not exist inside BUSINESS_ARTIFACT_ROOT.")
    sha256 = file_digest(resolved)
    return {
        "schema_version": 1,
        "kind": kind,
        "artifact_id": "artifact-" + hashlib.sha256(
            f"{kind}\0{relative_path}\0{sha256}".encode("utf-8")
        ).hexdigest()[:24],
        "relative_path": relative_path,
        "format": resolved.suffix.casefold().lstrip("."),
        "sha256": sha256,
        "size_bytes": resolved.stat().st_size,
    }


def blocked_artifact_output(exc: ArtifactPathError) -> dict[str, Any]:
    return {
        "status": "blocked_host_artifact_sink",
        "message": str(exc),
        "artifact_root_environment": ARTIFACT_ROOT_ENV,
    }


ARTIFACT_KINDS = {"bounded_artifact_reference", "exported_query_result"}
FORMAT_SUFFIXES = {
    ".csv", ".tsv", ".xlsx", ".xls", ".xlsb", ".parquet", ".json", ".jsonl", ".ndjson",
    ".sqlite", ".sqlite3", ".db", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff",
    ".docx", ".pptx", ".txt", ".md",
}


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise StageRuntimeError(f"Missing JSON file: {path}")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise StageRuntimeError(f"JSON exceeds bounded runtime limit: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StageRuntimeError(f"Invalid JSON file: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StageRuntimeError("Top-level JSON must be an object")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path, _relative_path = artifact_relative_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def contract_path() -> Path:
    return Path(__file__).resolve().parents[1] / "references" / "contract.json"


def contract_summary(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage_id": contract.get("stage_id"),
        "objective": contract.get("objective"),
        "outcome": contract.get("outcome"),
        "required_inputs": [
            {"name": item.get("name"), "formats": item.get("accepted_formats", []), "required": item.get("required")}
            for item in contract.get("input_contract", []) if isinstance(item, dict)
        ],
        "required_outputs": [
            {"name": item.get("name"), "formats": item.get("formats", []), "required": item.get("required")}
            for item in contract.get("output_contract", []) if isinstance(item, dict)
        ],
        "foundation_skills": contract.get("foundation_skills", []),
        "procedure": contract.get("procedure", []),
        "controls": contract.get("control_ids", []),
        "execution_contract": contract.get("execution_contract", {}),
    }


def validate_artifact_reference(value: Any, owner: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") not in ARTIFACT_KINDS:
        raise StageRuntimeError(
            f"{owner} must be a bounded_artifact_reference or exported_query_result"
        )
    if "path" in value:
        raise StageRuntimeError(
            f"{owner} uses a legacy physical artifact path; provide relative_path under BUSINESS_ARTIFACT_ROOT."
        )
    raw_relative_path = str(value.get("relative_path", "")).strip()
    expected = str(value.get("sha256", "")).strip().casefold()
    try:
        path, relative_path = resolve_artifact_name(raw_relative_path, f"{owner} relative_path")
    except ArtifactPathError as exc:
        raise StageRuntimeError(str(exc)) from exc
    if path.suffix.casefold() not in FORMAT_SUFFIXES:
        raise StageRuntimeError(f"{owner} has an invalid artifact format")
    if not path.is_file():
        raise StageRuntimeError(f"{owner} points to a missing artifact")
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise StageRuntimeError(f"{owner} has an invalid sha256")
    actual = file_digest(path)
    if actual != expected:
        raise StageRuntimeError(f"{owner} sha256 does not match the artifact on disk")
    kind = str(value.get("kind"))
    return {
        "schema_version": int(value.get("schema_version", 1)),
        "kind": kind,
        "artifact_id": str(value.get("artifact_id") or "artifact-" + hashlib.sha256(
            f"{kind}\0{relative_path}\0{actual}".encode("utf-8")
        ).hexdigest()[:24]),
        "relative_path": relative_path,
        "format": path.suffix.casefold().lstrip("."),
        "sha256": actual,
        "size_bytes": path.stat().st_size,
    }


def ensure_no_raw_data(value: Any, owner: str = "input") -> None:
    if isinstance(value, dict):
        if value.get("kind") in ARTIFACT_KINDS:
            validate_artifact_reference(value, owner)
            return
        for key, child in value.items():
            ensure_no_raw_data(child, f"{owner}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            ensure_no_raw_data(child, f"{owner}[{index}]")
        return
    if not isinstance(value, str):
        return
    candidate = Path(value)
    if candidate.suffix.casefold() not in RAW_DATA_EXTENSIONS or not candidate.exists():
        return
    raise StageRuntimeError(
        f"{owner} points to a raw business file ({candidate.suffix}); use the declared foundation Skill and pass a bounded artifact reference"
    )


def start_work_order(contract: dict[str, Any], request: str, input_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not request.strip():
        raise StageRuntimeError("Stage request cannot be empty")
    if input_payload is not None:
        ensure_no_raw_data(input_payload)
    summary = contract_summary(contract)
    payload = {
        "schema_version": 1,
        "status": "ready",
        "stage_id": contract.get("stage_id"),
        "request": request.strip(),
        "contract_digest": digest_payload(contract),
        "input": input_payload or {},
        "execution": summary,
        "rules": [
            "Use only declared foundation scripts for files, OCR, knowledge retrieval, and large-table queries.",
            "Do not write temporary Python or load raw business files into Agent context.",
            "Finish with scripts/run_stage.py finish and a bounded result JSON.",
        ],
    }
    payload["work_order_digest"] = digest_payload(payload)
    return payload


def finish_work_order(contract: dict[str, Any], work_order: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    if work_order.get("stage_id") != contract.get("stage_id"):
        raise StageRuntimeError("Work order belongs to another stage")
    if work_order.get("contract_digest") != digest_payload(contract):
        raise StageRuntimeError("Stage contract changed after the work order was created")
    expected_digest = str(work_order.get("work_order_digest", ""))
    unsigned = dict(work_order)
    unsigned.pop("work_order_digest", None)
    if not expected_digest or digest_payload(unsigned) != expected_digest:
        raise StageRuntimeError("Work order digest mismatch")
    if result.get("status") != "complete":
        raise StageRuntimeError("Result status must be complete")
    outputs = result.get("outputs")
    if not isinstance(outputs, list):
        raise StageRuntimeError("Result outputs must be an array")
    if any(not isinstance(item, dict) for item in outputs):
        raise StageRuntimeError("Each result output must be an object")
    names = {str(item.get("name", "")).strip() for item in outputs}
    if "" in names:
        raise StageRuntimeError("Each result output must have a name")
    required = {
        str(item.get("name", ""))
        for item in contract.get("output_contract", [])
        if isinstance(item, dict) and item.get("required") is True
    }
    missing = sorted(required - names)
    if missing:
        raise StageRuntimeError(f"Missing required outputs: {missing}")
    contract_outputs = {
        str(item.get("name", "")): item
        for item in contract.get("output_contract", [])
        if isinstance(item, dict)
    }
    unknown = sorted(names - set(contract_outputs))
    if unknown:
        raise StageRuntimeError(f"Outputs are outside the stage contract: {unknown}")
    normalized_outputs: list[dict[str, Any]] = []
    for output in outputs:
        output = dict(output)
        spec = contract_outputs.get(str(output.get("name", "")), {})
        value = output.get("value", output.get("artifact"))
        formats = {
            str(item).casefold() if str(item).startswith(".") else f".{str(item).casefold()}"
            for item in spec.get("formats", [])
            if str(item).strip()
        }
        if formats.intersection(FORMAT_SUFFIXES):
            if not isinstance(value, dict) or value.get("kind") not in ARTIFACT_KINDS:
                raise StageRuntimeError(
                    f"Output {output.get('name', '')} must provide a verifiable artifact reference"
                )
            artifact = validate_artifact_reference(value, f"output {output.get('name', '')}")
            suffix = f".{artifact['format']}"
            if suffix not in formats:
                raise StageRuntimeError(
                    f"Output {output.get('name', '')} format {suffix} is outside the declared contract"
                )
            if "value" in output:
                output["value"] = artifact
            else:
                output["artifact"] = artifact
        elif value is None and "value" not in output and "artifact" not in output:
            raise StageRuntimeError(f"Output {output.get('name', '')} has no bounded value")
        normalized_outputs.append(output)
    ensure_no_raw_data(result, "result")
    handoff = {
        "schema_version": 1,
        "status": "complete",
        "stage_id": contract.get("stage_id"),
        "work_order_digest": expected_digest,
        "outputs": normalized_outputs,
        "evidence": result.get("evidence", []),
        "uncertainties": result.get("uncertainties", []),
    }
    handoff["handoff_digest"] = digest_payload(handoff)
    return handoff


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("contract")
    start = commands.add_parser("start")
    start.add_argument("--request", required=True)
    start.add_argument("--input")
    start.add_argument("--output", required=True, help="Safe relative artifact name under BUSINESS_ARTIFACT_ROOT.")
    finish = commands.add_parser("finish")
    finish.add_argument("--work-order", required=True)
    finish.add_argument("--result", required=True)
    finish.add_argument("--output", required=True, help="Safe relative artifact name under BUSINESS_ARTIFACT_ROOT.")
    return parser


def run(argv: Sequence[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = build_parser().parse_args(argv)
    contract = read_json(contract_path())
    if args.command == "contract":
        return 0, contract_summary(contract)
    if args.command == "start":
        input_payload = read_json(Path(args.input).resolve()) if args.input else None
        payload = start_work_order(contract, args.request, input_payload)
        try:
            output_path, _output_relative_path = resolve_artifact_name(args.output, "--output")
        except ArtifactPathError as exc:
            return 2, blocked_artifact_output(exc)
        write_json(output_path, payload)
        artifact = artifact_reference(output_path, "stage_work_order")
        return 0, {
            "status": "ready", "stage_id": payload["stage_id"],
            "artifact": artifact, "artifact_id": artifact["artifact_id"],
            "relative_path": artifact["relative_path"],
        }
    work_order = read_json(Path(args.work_order).resolve())
    result = read_json(Path(args.result).resolve())
    payload = finish_work_order(contract, work_order, result)
    try:
        output_path, _output_relative_path = resolve_artifact_name(args.output, "--output")
    except ArtifactPathError as exc:
        return 2, blocked_artifact_output(exc)
    write_json(output_path, payload)
    artifact = artifact_reference(output_path, "stage_handoff")
    return 0, {
        "status": "complete", "stage_id": payload["stage_id"],
        "artifact": artifact, "artifact_id": artifact["artifact_id"],
        "relative_path": artifact["relative_path"], "handoff_digest": payload["handoff_digest"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        code, payload = run(argv)
    except (StageRuntimeError, OSError, ValueError) as exc:
        code, payload = 2, {"status": "blocked", "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
