#!/usr/bin/env python3
"""Standalone entry point for the generic result-anchored trace engine.

The original prototype in this location depended on another project's domain
models and encoded fixed ID/category/time heuristics.  The maintained
implementation now lives with ``discover-data-relations`` so the command-line
entry point and the production pipeline use exactly the same algorithm.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
SCRIPT_ROOT = PROJECT_ROOT / "system_skills" / "discover-data-relations" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from trace_engine import build_trace_samples, compact_trace_report  # noqa: E402


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_cards(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    payload = load_json(path)
    values = payload.get("cards", []) if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise ValueError("Evidence cards must be a JSON list or an object containing 'cards'.")
    return [item for item in values if isinstance(item, dict)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Trace coherent business records backwards from inferred result rows. "
            "Full sources are searched, but only bounded redacted rows are emitted."
        )
    )
    parser.add_argument("--input", required=True, help="Root directory containing the source files")
    parser.add_argument(
        "--field-result", required=True,
        help="Completed field-level relations.json from discover-data-relations",
    )
    parser.add_argument("--cards", help="Optional evidence-cards.json for result/rule semantics")
    parser.add_argument("--output", required=True, help="Destination trace-samples.json")
    parser.add_argument("--result-candidates", type=int, default=3)
    parser.add_argument("--anchor-candidates", type=int, default=6)
    parser.add_argument("--rows-per-source", type=int, default=8)
    parser.add_argument("--columns-per-source", type=int, default=48)
    parser.add_argument("--max-hops", type=int, default=4)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_root = Path(args.input).resolve()
    field_path = Path(args.field_result).resolve()
    output_path = Path(args.output).resolve()
    if not input_root.is_dir():
        raise SystemExit(f"Input directory does not exist: {input_root}")
    if not field_path.is_file():
        raise SystemExit(f"Field evidence does not exist: {field_path}")
    cards_path = Path(args.cards).resolve() if args.cards else None
    if cards_path is not None and not cards_path.is_file():
        raise SystemExit(f"Evidence cards do not exist: {cards_path}")

    report = build_trace_samples(
        input_root,
        load_json(field_path),
        load_cards(cards_path),
        result_candidate_limit=max(1, args.result_candidates),
        anchor_candidates=max(1, args.anchor_candidates),
        max_rows_per_source=max(1, args.rows_per_source),
        max_columns_per_source=max(1, args.columns_per_source),
        max_hops=max(1, args.max_hops),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(output_path)
    print(json.dumps(compact_trace_report(report), ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_trace_samples", "compact_trace_report", "main"]
