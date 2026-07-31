#!/usr/bin/env python3
"""Stable CLI over the inherited vector-kb client."""

from __future__ import annotations

import argparse
import json
from typing import Any, Sequence

from kb_client import get_chunk_source, search_kb


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=5)
    search.add_argument(
        "--required",
        action="store_true",
        help="Return manual_intervention_required when mandatory knowledge cannot be retrieved",
    )
    source = commands.add_parser("source")
    source.add_argument("--document-id", required=True)
    source.add_argument("--chunk-id")
    return parser


def run(argv: Sequence[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "search":
            payload = search_kb(args.query, limit=max(1, min(args.limit, 20)))
        else:
            payload = get_chunk_source(args.document_id, args.chunk_id)
    except Exception as exc:  # noqa: BLE001 - normalize provider failures at the portable CLI boundary
        payload = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    status = str(payload.get("status", "error"))
    if args.command == "search" and args.required and status != "success":
        return 3, {
            "status": "manual_intervention_required",
            "reason": "mandatory_external_knowledge_unavailable_or_empty",
            "provider_status": status,
            "message": str(payload.get("message") or "Required knowledge returned no usable results."),
            "next_action": "Ask a human to provide or verify the required external knowledge before deciding.",
        }
    return (0 if status in {"success", "no_results"} else 2), payload


def main(argv: Sequence[str] | None = None) -> int:
    try:
        code, payload = run(argv)
    except (OSError, ValueError) as exc:
        code, payload = 2, {"status": "error", "message": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
