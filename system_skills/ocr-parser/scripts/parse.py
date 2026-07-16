"""
parse.py — OCR 文件解析命令行入口

用法：
    # 解析远程 URL（单个或逗号分隔多个）
    python parse.py --url "https://example.com/report.pdf"
    python parse.py --url "https://example.com/a.pdf,https://example.com/b.png"

    # 解析本地文件路径（单个或逗号分隔多个）
    python parse.py --path "/tmp/scan.pdf"
    python parse.py --path "/tmp/doc1.pdf,/tmp/doc2.png"

    # 解析 Base64 编码内容（需同时传 --name 指定文件名）
    python parse.py --b64 "<base64string>" --name "report.pdf"

    # 输出格式（默认 text，可选 json）
    python parse.py --path "/tmp/scan.pdf" --format json

退出码：
    0  全部解析成功
    1  全部失败或发生异常
    2  部分失败
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 将 scripts 目录的父目录加入路径，确保相对导入正常工作
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.ocr_service import get_ocr_service


def parse_targets(args: argparse.Namespace) -> list[dict]:
    """根据命令行参数逐一调用 OCR 服务，返回结果列表。"""
    service = get_ocr_service()
    results: list[dict] = []

    # 1. 远程 URL（支持批量，逗号分隔）
    if args.url:
        for url in [u.strip() for u in args.url.split(",") if u.strip()]:
            result = service.parse(file_url=url)
            results.append({"source": url, **result})

    # 2. 本地路径（支持批量，逗号分隔）
    if args.path:
        for path in [p.strip() for p in args.path.split(",") if p.strip()]:
            result = service.parse(file_path=path, file_name=Path(path).name)
            results.append({"source": path, **result})

    # 3. Base64 内容（单文件）
    if args.b64:
        file_name = args.name or "file.bin"
        result = service.parse(file_content_b64=args.b64, file_name=file_name)
        results.append({"source": file_name, **result})

    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="通过 OCR 服务解析文件，提取文本内容"
    )
    parser.add_argument("--url",    help="远程文件 URL，多个用英文逗号分隔")
    parser.add_argument("--path",   help="本地文件路径，多个用英文逗号分隔")
    parser.add_argument("--b64",    help="Base64 编码的文件内容（单文件）")
    parser.add_argument("--name",   help="文件名（配合 --b64 使用，用于类型推断）")
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="输出格式：text（默认）或 json",
    )
    args = parser.parse_args()

    if not any([args.url, args.path, args.b64]):
        parser.print_help()
        return 1

    results = parse_targets(args)

    if not results:
        print("错误：未产生任何解析结果", file=sys.stderr)
        return 1

    # ── 输出 ──────────────────────────────────
    if args.format == "json":
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for item in results:
            if len(results) > 1:
                print(f"=== {item['source']} ===")
            if item["status"] == "success":
                print(item["text"])
            else:
                print(f"[解析失败] {item.get('message', '')}", file=sys.stderr)
            if len(results) > 1:
                print()

    # ── 退出码 ────────────────────────────────
    success_count = sum(1 for r in results if r["status"] == "success")
    if success_count == len(results):
        return 0
    elif success_count == 0:
        return 1
    else:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
