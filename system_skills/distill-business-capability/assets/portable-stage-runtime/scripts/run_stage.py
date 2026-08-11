#!/usr/bin/env python3
"""Create and validate bounded work orders for one distilled business stage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Sequence


MAX_JSON_BYTES = 4 * 1024 * 1024
RAW_DATA_EXTENSIONS = {
    ".csv", ".tsv", ".xlsx", ".xls", ".xlsb", ".parquet", ".jsonl", ".ndjson",
    ".sqlite", ".sqlite3", ".db", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff",
    ".docx", ".pptx",
}


class StageRuntimeError(ValueError):
    pass


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
    raw_path = str(value.get("path", "")).strip()
    expected = str(value.get("sha256", "")).strip().casefold()
    path = Path(raw_path).expanduser()
    if not raw_path or path.suffix.casefold() not in FORMAT_SUFFIXES:
        raise StageRuntimeError(f"{owner} has an invalid artifact path")
    if not path.is_file():
        raise StageRuntimeError(f"{owner} points to a missing artifact: {raw_path}")
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise StageRuntimeError(f"{owner} has an invalid sha256")
    actual = file_digest(path)
    if actual != expected:
        raise StageRuntimeError(f"{owner} sha256 does not match the artifact on disk")
    result = dict(value)
    result["path"] = str(path.resolve())
    result["sha256"] = actual
    result.setdefault("format", path.suffix.casefold().lstrip("."))
    return result


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
    for output in outputs:
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
            suffix = Path(artifact["path"]).suffix.casefold()
            if suffix not in formats:
                raise StageRuntimeError(
                    f"Output {output.get('name', '')} format {suffix} is outside the declared contract"
                )
        elif value is None and "value" not in output and "artifact" not in output:
            raise StageRuntimeError(f"Output {output.get('name', '')} has no bounded value")
    ensure_no_raw_data(result, "result")
    handoff = {
        "schema_version": 1,
        "status": "complete",
        "stage_id": contract.get("stage_id"),
        "work_order_digest": expected_digest,
        "outputs": outputs,
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
    start.add_argument("--output", required=True)
    finish = commands.add_parser("finish")
    finish.add_argument("--work-order", required=True)
    finish.add_argument("--result", required=True)
    finish.add_argument("--output", required=True)
    return parser


def run(argv: Sequence[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = build_parser().parse_args(argv)
    contract = read_json(contract_path())
    if args.command == "contract":
        return 0, contract_summary(contract)
    if args.command == "start":
        input_payload = read_json(Path(args.input).resolve()) if args.input else None
        payload = start_work_order(contract, args.request, input_payload)
        write_json(Path(args.output).resolve(), payload)
        return 0, {"status": "ready", "stage_id": payload["stage_id"], "output": str(Path(args.output).resolve())}
    work_order = read_json(Path(args.work_order).resolve())
    result = read_json(Path(args.result).resolve())
    payload = finish_work_order(contract, work_order, result)
    write_json(Path(args.output).resolve(), payload)
    return 0, {"status": "complete", "stage_id": payload["stage_id"], "output": str(Path(args.output).resolve()), "handoff_digest": payload["handoff_digest"]}


def main(argv: Sequence[str] | None = None) -> int:
    try:
        code, payload = run(argv)
    except (StageRuntimeError, OSError, ValueError) as exc:
        code, payload = 2, {"status": "blocked", "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
