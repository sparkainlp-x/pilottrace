#!/usr/bin/env python3
"""Standalone offline validator and scorer for caller-mapped numeric trace data."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FRAME_WIDTH = 512
BLOCK_WIDTH = 32
BLOCK_COUNT = FRAME_WIDTH // BLOCK_WIDTH
WEIGHTS = (0.45, 0.35, 0.20)
SCORING_VERSION = "pilottrace-oes32-v1"
SCORING_FORMULA = "0.45*max(abs(x)) + 0.35*RMS(x) + 0.20*mean(abs(x))"
REPORT_SCHEMA_VERSION = 1
METADATA_FIELDS = (
    "channel_order",
    "units",
    "scaling",
    "timebase",
    "baseline_preprocessing",
    "missing_value_policy",
    "source_provenance",
    "permitted_use",
)
_PLACEHOLDERS = {
    "?", "na", "n/a", "none", "null", "unknown", "unspecified",
    "not specified", "not provided", "tbd", "todo", "pending",
}


class PilotTraceError(ValueError):
    """Base class for input or manifest contract errors."""


class TraceFormatError(PilotTraceError):
    """The trace document cannot be processed as a trace."""


class ManifestError(PilotTraceError):
    """The manifest is malformed or lacks required declarations."""


@dataclass(frozen=True)
class AcceptedFrame:
    frame_index: int
    values: tuple[float, ...]
    label: bool | None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _parse_constant(token: str) -> float:
    # Keep non-standard numeric tokens visible to per-frame finite-value checks.
    return {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}[token]


def _load_json(data: bytes, description: str, error_type: type[PilotTraceError]) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise error_type(f"{description} is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_parse_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise error_type(f"could not parse {description} JSON: {exc}") from exc


def load_manifest_bytes(data: bytes) -> tuple[dict[str, str], float]:
    """Validate a complete manifest and return declarations and frozen threshold."""
    obj = _load_json(data, "manifest", ManifestError)
    if not isinstance(obj, dict):
        raise ManifestError("manifest root must be a JSON object")
    expected_keys = {"schema_version", "threshold", *METADATA_FIELDS}
    missing = sorted(expected_keys - set(obj))
    extra = sorted(set(obj) - expected_keys)
    if missing:
        raise ManifestError("manifest is missing required field(s): " + ", ".join(missing))
    if extra:
        raise ManifestError("manifest has unknown field(s): " + ", ".join(extra))
    if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:
        raise ManifestError("manifest.schema_version must be integer 1")

    raw_threshold = obj["threshold"]
    if isinstance(raw_threshold, bool) or not isinstance(raw_threshold, (int, float)):
        raise ManifestError("manifest.threshold must be a finite non-negative number")
    try:
        threshold = float(raw_threshold)
    except (OverflowError, ValueError) as exc:
        raise ManifestError("manifest.threshold must be a finite non-negative number") from exc
    if not math.isfinite(threshold) or threshold < 0:
        raise ManifestError("manifest.threshold must be a finite non-negative number")

    metadata: dict[str, str] = {}
    for field in METADATA_FIELDS:
        value = obj[field]
        if not isinstance(value, str) or not value.strip():
            raise ManifestError(f"manifest.{field} must be a non-empty string")
        normalized = value.strip()
        if normalized.casefold() in _PLACEHOLDERS:
            raise ManifestError(f"manifest.{field} must be explicit, not a placeholder")
        metadata[field] = normalized
    return metadata, threshold


def _validate_frame(obj: Any, frame_index: int) -> tuple[AcceptedFrame | None, list[str]]:
    where = f"frame[{frame_index}]"
    reasons: list[str] = []
    if not isinstance(obj, dict):
        return None, [f"{where} must be a JSON object"]

    extras = sorted(set(obj) - {"values", "label"})
    if extras:
        reasons.append(f"{where} has unknown field(s): {', '.join(extras)}")
    if "values" not in obj:
        reasons.append(f"{where}.values is required")
        values: Any = None
    else:
        values = obj["values"]

    parsed_values: list[float] = []
    if not isinstance(values, list):
        if "values" in obj:
            reasons.append(f"{where}.values must be an array")
    else:
        if len(values) != FRAME_WIDTH:
            reasons.append(
                f"{where}.values must contain exactly {FRAME_WIDTH} values (got {len(values)})"
            )
        for channel_index, value in enumerate(values):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                reasons.append(
                    f"{where}.values[{channel_index}] must be a finite numeric real value "
                    "(booleans are not accepted)"
                )
                continue
            try:
                converted = float(value)
            except (OverflowError, ValueError):
                reasons.append(
                    f"{where}.values[{channel_index}] is outside the supported finite numeric range"
                )
                continue
            if not math.isfinite(converted):
                reasons.append(
                    f"{where}.values[{channel_index}] must be finite (NaN and infinity are not accepted)"
                )
                continue
            parsed_values.append(converted)

    label: bool | None = None
    if "label" in obj:
        if type(obj["label"]) is not bool:
            reasons.append(f"{where}.label must be a boolean when present")
        else:
            label = obj["label"]

    if reasons:
        return None, reasons
    return AcceptedFrame(frame_index, tuple(parsed_values), label), []


def load_trace_bytes(data: bytes) -> tuple[list[AcceptedFrame], list[dict[str, Any]], int]:
    """Validate trace structure and return accepted frames and rejection details."""
    obj = _load_json(data, "trace", TraceFormatError)
    if not isinstance(obj, dict):
        raise TraceFormatError("trace root must be a JSON object with a 'frames' array")
    if set(obj) != {"frames"}:
        missing = sorted({"frames"} - set(obj))
        extra = sorted(set(obj) - {"frames"})
        details = []
        if missing:
            details.append("missing required field(s): " + ", ".join(missing))
        if extra:
            details.append("unknown field(s): " + ", ".join(extra))
        raise TraceFormatError("trace " + "; ".join(details))
    raw_frames = obj["frames"]
    if not isinstance(raw_frames, list):
        raise TraceFormatError("trace.frames must be an array")
    if not raw_frames:
        raise TraceFormatError("trace.frames must contain at least one frame")

    accepted: list[AcceptedFrame] = []
    rejected: list[dict[str, Any]] = []
    for index, raw_frame in enumerate(raw_frames):
        parsed, reasons = _validate_frame(raw_frame, index)
        if parsed is None:
            rejected.append({"frame_index": index, "reasons": reasons})
        else:
            accepted.append(parsed)
    return accepted, rejected, len(raw_frames)


def score_block(values: list[float] | tuple[float, ...]) -> float:
    """Score one 32-value block using scale-safe intermediate arithmetic."""
    if len(values) != BLOCK_WIDTH:
        raise ValueError(f"block must contain exactly {BLOCK_WIDTH} values")
    magnitudes = [abs(float(value)) for value in values]
    peak = max(magnitudes)
    if peak == 0.0:
        return 0.0
    scaled = [magnitude / peak for magnitude in magnitudes]
    rms_scaled = math.sqrt(math.fsum(value * value for value in scaled) / BLOCK_WIDTH)
    mean_scaled = math.fsum(scaled) / BLOCK_WIDTH
    normalized_score = (
        WEIGHTS[0] + WEIGHTS[1] * rms_scaled + WEIGHTS[2] * mean_scaled
    )
    return peak * min(normalized_score, 1.0)


def score_frame(values: list[float] | tuple[float, ...]) -> list[float]:
    """Score the 16 contiguous 32-value blocks of one frame."""
    if len(values) != FRAME_WIDTH:
        raise ValueError(f"frame must contain exactly {FRAME_WIDTH} values")
    return [
        score_block(values[start : start + BLOCK_WIDTH])
        for start in range(0, FRAME_WIDTH, BLOCK_WIDTH)
    ]


def _divide(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _classification_metrics(frames: list[dict[str, Any]]) -> dict[str, Any]:
    labeled = [frame for frame in frames if frame["label"] is not None]
    if not labeled:
        return {
            "status": "not_computed",
            "reason": "No accepted frame carried a boolean label.",
        }

    tp = sum(frame["label"] is True and frame["flagged"] is True for frame in labeled)
    tn = sum(frame["label"] is False and frame["flagged"] is False for frame in labeled)
    fp = sum(frame["label"] is False and frame["flagged"] is True for frame in labeled)
    fn = sum(frame["label"] is True and frame["flagged"] is False for frame in labeled)
    total = len(labeled)
    return {
        "status": "computed_descriptive_only",
        "classification_unit": "accepted frame with a supplied boolean label",
        "positive_label": "label is true",
        "predicted_positive": "one or more of the frame's 16 blocks is flagged",
        "labeled_frame_count": total,
        "confusion_counts": {
            "true_positive": tp,
            "true_negative": tn,
            "false_positive": fp,
            "false_negative": fn,
        },
        "descriptive_accuracy": _divide(tp + tn, total),
        "descriptive_precision": _divide(tp, tp + fp),
        "descriptive_recall_sensitivity": _divide(tp, tp + fn),
        "descriptive_specificity": _divide(tn, tn + fp),
        "descriptive_f1_score": _divide(2 * tp, 2 * tp + fp + fn),
        "interpretation": (
            "Descriptive results on the supplied labeled frames only; not an estimate "
            "of performance on other data."
        ),
    }


def analyze_bytes(trace_bytes: bytes, manifest_bytes: bytes) -> dict[str, Any]:
    """Validate and score exact input bytes without device or network access."""
    metadata, numeric_threshold = load_manifest_bytes(manifest_bytes)
    accepted, rejected, total_frames = load_trace_bytes(trace_bytes)

    frame_results: list[dict[str, Any]] = []
    flagged_blocks: list[dict[str, Any]] = []
    for frame in accepted:
        scores = score_frame(frame.values)
        block_results: list[dict[str, Any]] = []
        flagged_block_indices: list[int] = []
        for block_index, score in enumerate(scores):
            is_flagged = score >= numeric_threshold
            start = block_index * BLOCK_WIDTH
            end = (block_index + 1) * BLOCK_WIDTH
            block_results.append({
                "block_index": block_index,
                "channel_range_start_inclusive": start,
                "channel_range_end_exclusive": end,
                "score": score,
                "flagged": is_flagged,
            })
            if is_flagged:
                flagged_block_indices.append(block_index)
                flagged_blocks.append({
                    "frame_index": frame.frame_index,
                    "block_index": block_index,
                    "score": score,
                    "channel_range_start_inclusive": start,
                    "channel_range_end_exclusive": end,
                })
        frame_results.append({
            "frame_index": frame.frame_index,
            "label": frame.label,
            "flagged": bool(flagged_block_indices),
            "flagged_block_indices": flagged_block_indices,
            "blocks": block_results,
        })

    labeled_count = sum(frame.label is not None for frame in accepted)
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "prototype": "PilotTrace illustrative offline prototype",
        "scoring_version": SCORING_VERSION,
        "scoring_formula": SCORING_FORMULA,
        "threshold": numeric_threshold,
        "threshold_comparison": "score >= threshold",
        "frame_flag_rule": "A valid frame is flagged when at least one of its 16 blocks is flagged.",
        "hashes": {
            "trace_input_sha256": hashlib.sha256(trace_bytes).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
        "manifest_metadata": metadata,
        "input_summary": {
            "frames_total": total_frames,
            "frames_accepted": len(accepted),
            "frames_rejected": len(rejected),
            "accepted_labeled_frames": labeled_count,
            "accepted_unlabeled_frames": len(accepted) - labeled_count,
            "flagged_frames": sum(frame["flagged"] for frame in frame_results),
            "flagged_blocks": len(flagged_blocks),
        },
        "rejected_frames": rejected,
        "frames": frame_results,
        "flagged_blocks": flagged_blocks,
        "labeled_frame_classification": _classification_metrics(frame_results),
        "scope_note": (
            "Offline processing of caller-mapped numeric data only. This prototype is not "
            "validated operational detection and does not provide alerts or control functions."
        ),
    }


def analyze_files(trace_path: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    """Read local trace and manifest files and analyze their exact bytes."""
    try:
        trace_bytes = Path(trace_path).read_bytes()
    except OSError as exc:
        raise TraceFormatError(f"cannot read trace file {trace_path}: {exc}") from exc
    try:
        manifest_bytes = Path(manifest_path).read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest file {manifest_path}: {exc}") from exc
    return analyze_bytes(trace_bytes, manifest_bytes)


def report_json(report: dict[str, Any]) -> str:
    """Serialize a report deterministically as JSON text."""
    return json.dumps(
        report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
    ) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pilottrace_standalone.py",
        description="Validate and score caller-mapped numeric JSON trace frames.",
    )
    parser.add_argument("--trace", required=True, help="path to a UTF-8 trace JSON file")
    parser.add_argument("--manifest", required=True, help="path to a UTF-8 JSON manifest")
    parser.add_argument(
        "--output", default="-",
        help="report JSON destination; use '-' or omit to print to standard output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        text = report_json(analyze_files(args.trace, args.manifest))
        if args.output == "-":
            sys.stdout.write(text)
        else:
            Path(args.output).write_text(text, encoding="utf-8", newline="\n")
    except (PilotTraceError, OSError, ValueError) as exc:
        print(f"pilottrace_standalone.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
