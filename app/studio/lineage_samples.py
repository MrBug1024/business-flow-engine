"""Readable, current data-lineage sample views for a Studio workspace.

The canonical ``trace-samples.json`` remains the audit contract.  This module
derives small CSV and Markdown views from that contract so people can inspect
the exact rows and document excerpts without reading internal JSON.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


SAMPLE_ROOT_RELATIVE = "outputs/data-lineage-samples"
SAMPLE_INDEX_RELATIVE = f"{SAMPLE_ROOT_RELATIVE}/index.json"


def materialize_lineage_samples(
    workspace: Path,
    trace: Mapping[str, Any],
) -> dict[str, Any]:
    """Create immutable, human-readable views for one accepted trace.

    Every view name carries the current trace fingerprint.  This avoids
    silently overwriting an artifact a reviewer may still have open, while the
    index points the data catalog at only the current trace.
    """

    trace_bytes = json.dumps(
        trace,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = hashlib.sha256(trace_bytes).hexdigest()
    root = (workspace / SAMPLE_ROOT_RELATIVE).resolve()
    workspace = workspace.resolve()
    if root != workspace and workspace not in root.parents:
        raise ValueError("Data-lineage sample directory is outside the workspace.")
    root.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    bundles = trace.get("bundles")
    if not isinstance(bundles, list):
        bundles = []
    for bundle_index, bundle in enumerate(bundles, 1):
        if not isinstance(bundle, Mapping):
            continue
        bundle_id = _text(bundle.get("bundle_id")) or f"bundle-{bundle_index}"
        anchor = _mapping(bundle.get("anchor"))
        for source_index, source in enumerate(_mappings(bundle.get("sources")), 1):
            record = _materialize_tabular_source(
                root=root,
                fingerprint=fingerprint,
                bundle_id=bundle_id,
                anchor=anchor,
                source=source,
                ordinal=len(records) + source_index,
            )
            if record is not None:
                records.append(record)
        for evidence_index, evidence in enumerate(_mappings(bundle.get("semantic_evidence")), 1):
            record = _materialize_semantic_evidence(
                root=root,
                fingerprint=fingerprint,
                bundle_id=bundle_id,
                anchor=anchor,
                evidence=evidence,
                ordinal=len(records) + evidence_index,
            )
            if record is not None:
                records.append(record)

    index = {
        "schema_version": 1,
        "trace_fingerprint": fingerprint,
        "trace_status": _text(trace.get("status")),
        "samples": records,
    }
    _atomic_write_json(root / "index.json", index)
    _write_readme(root, index)
    return {
        "root": SAMPLE_ROOT_RELATIVE,
        "index": SAMPLE_INDEX_RELATIVE,
        "trace_fingerprint": fingerprint,
        "sample_count": len(records),
        "samples": records,
    }


def load_current_lineage_samples(workspace: Path) -> dict[str, Any]:
    """Read the current sample index without exposing malformed artifacts."""

    path = (workspace.resolve() / SAMPLE_INDEX_RELATIVE).resolve()
    if not path.is_file():
        return {"root": SAMPLE_ROOT_RELATIVE, "index": SAMPLE_INDEX_RELATIVE, "samples": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"root": SAMPLE_ROOT_RELATIVE, "index": SAMPLE_INDEX_RELATIVE, "samples": []}
    if not isinstance(payload, dict):
        return {"root": SAMPLE_ROOT_RELATIVE, "index": SAMPLE_INDEX_RELATIVE, "samples": []}
    samples = payload.get("samples")
    if not isinstance(samples, list):
        samples = []
    valid: list[dict[str, Any]] = []
    root = workspace.resolve()
    for raw in samples:
        if not isinstance(raw, dict):
            continue
        relative = _text(raw.get("path")).replace("\\", "/").strip("/")
        if not relative:
            continue
        candidate = (root / relative).resolve()
        if candidate.is_file() and root in candidate.parents:
            valid.append(dict(raw, path=relative))
    return {
        "root": SAMPLE_ROOT_RELATIVE,
        "index": SAMPLE_INDEX_RELATIVE,
        "trace_fingerprint": _text(payload.get("trace_fingerprint")),
        "trace_status": _text(payload.get("trace_status")),
        "samples": valid,
    }


def _materialize_tabular_source(
    *,
    root: Path,
    fingerprint: str,
    bundle_id: str,
    anchor: Mapping[str, Any],
    source: Mapping[str, Any],
    ordinal: int,
) -> dict[str, Any] | None:
    source_path = _text(source.get("path"))
    source_table = _text(source.get("table"))
    role = _text(source.get("role"))
    rows = _mappings(source.get("rows"))
    if not source_path or not rows:
        return None
    fields: list[str] = []
    for row in rows:
        values = _mapping(row.get("values"))
        for field in values:
            field_name = _text(field)
            if field_name and field_name not in fields:
                fields.append(field_name)
    if not fields:
        return None
    filename = _sample_filename(ordinal, source_path, source_table or "file", fingerprint, "csv")
    destination = root / filename
    metadata = [
        "trace_bundle_id",
        "trace_role",
        "trace_source_path",
        "trace_table",
        "trace_row_number",
    ]
    with destination.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[*metadata, *fields], extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            values = _mapping(row.get("values"))
            writer.writerow({
                "trace_bundle_id": bundle_id,
                "trace_role": role,
                "trace_source_path": source_path,
                "trace_table": source_table,
                "trace_row_number": _text(row.get("row_number")),
                **{field: _csv_value(values.get(field)) for field in fields},
            })
    label = f"{Path(source_path).name} · {source_table or 'file'}"
    return {
        "id": f"{bundle_id}:{source_path}:{source_table}:rows",
        "type": "table",
        "path": f"{SAMPLE_ROOT_RELATIVE}/{filename}",
        "label": label,
        "source_path": source_path,
        "source_table": source_table,
        "role": role,
        "row_count": len(rows),
        "anchor": _anchor_payload(anchor),
    }


def _materialize_semantic_evidence(
    *,
    root: Path,
    fingerprint: str,
    bundle_id: str,
    anchor: Mapping[str, Any],
    evidence: Mapping[str, Any],
    ordinal: int,
) -> dict[str, Any] | None:
    source_path = _text(evidence.get("path"))
    locator = _text(evidence.get("locator"))
    snippet = _text(evidence.get("snippet"))
    if not source_path or not snippet:
        return None
    filename = _sample_filename(ordinal, source_path, "evidence", fingerprint, "md")
    destination = root / filename
    destination.write_text(
        "\n".join([
            f"# {Path(source_path).name}",
            "",
            f"- Trace bundle: `{bundle_id}`",
            f"- Source: `{source_path}`",
            f"- Location: `{locator or 'not provided'}`",
            f"- Result anchor: `{_anchor_label(anchor)}`",
            "",
            "## Matched evidence",
            "",
            snippet,
            "",
        ]),
        encoding="utf-8",
    )
    return {
        "id": f"{bundle_id}:{source_path}:{locator}:evidence",
        "type": "document",
        "path": f"{SAMPLE_ROOT_RELATIVE}/{filename}",
        "label": f"{Path(source_path).name} · matched evidence",
        "source_path": source_path,
        "source_table": _text(evidence.get("table")),
        "role": "semantic_evidence",
        "locator": locator,
        "anchor": _anchor_payload(anchor),
    }


def _sample_filename(ordinal: int, source_path: str, scope: str, fingerprint: str, suffix: str) -> str:
    identity = f"{source_path}\0{scope}".encode("utf-8")
    source_hash = hashlib.sha256(identity).hexdigest()[:8]
    return f"{ordinal:02d}-{_slug(Path(source_path).stem)}-{_slug(scope)}-{source_hash}-{fingerprint[:10]}.{suffix}"


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return normalized[:48] or "source"


def _anchor_label(anchor: Mapping[str, Any]) -> str:
    if _text(anchor.get("kind")) == "document_segment":
        return ":".join([
            _text(anchor.get("path")) or "result",
            _text(anchor.get("locator")) or "segment",
        ])
    return ":".join([
        _text(anchor.get("path")) or "result",
        _text(anchor.get("table")) or "table",
        _text(anchor.get("row_number")) or "row",
    ])


def _anchor_payload(anchor: Mapping[str, Any]) -> dict[str, Any]:
    if _text(anchor.get("kind")) == "document_segment":
        return {
            "path": _text(anchor.get("path")),
            "kind": "document_segment",
            "locator": _text(anchor.get("locator")),
            "source_digest": _text(anchor.get("source_digest")),
            "segment_digest": _text(anchor.get("segment_digest")),
        }
    return {
        "path": _text(anchor.get("path")),
        "table": _text(anchor.get("table")),
        "row_number": anchor.get("row_number"),
    }


def _write_readme(root: Path, index: Mapping[str, Any]) -> None:
    samples = _mappings(index.get("samples"))
    lines = [
        "# Data lineage samples",
        "",
        "This folder contains readable views of the current result-anchored trace.",
        "The source values are copied only from the bounded, audited trace artifact.",
        "",
        f"Current trace fingerprint: `{_text(index.get('trace_fingerprint'))}`",
        "",
        "## Samples",
        "",
    ]
    if samples:
        lines.extend(
            f"- `{_text(sample.get('path')).split('/')[-1]}`: {_text(sample.get('label'))}"
            for sample in samples
        )
    else:
        lines.append("No readable sample rows or document excerpts were generated for this trace.")
    lines.append("")
    (root / "README.md").write_text("\n".join(lines), encoding="utf-8")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)
