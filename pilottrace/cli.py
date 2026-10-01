"""Command-line interface for PilotTrace."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .api import analyze_files, report_json
from .validation import PilotTraceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pilottrace",
        description="Offline scoring of caller-mapped 512-value JSON trace frames.",
    )
    parser.add_argument("--trace", required=True, help="path to a UTF-8 trace JSON file")
    parser.add_argument("--manifest", required=True, help="path to a UTF-8 JSON manifest")
    parser.add_argument(
        "--output", default="-",
        help="report JSON destination; use '-' or omit to print to stdout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = analyze_files(args.trace, args.manifest)
        text = report_json(report)
        if args.output == "-":
            sys.stdout.write(text)
        else:
            Path(args.output).write_text(text, encoding="utf-8", newline="\n")
    except (PilotTraceError, OSError, ValueError) as exc:
        print(f"pilottrace: {exc}", file=sys.stderr)
        return 2
    return 0
