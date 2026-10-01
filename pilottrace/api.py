"""Public offline PilotTrace API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .scoring import SCORING_FORMULA, SCORING_VERSION, score_frame
from .validation import (
    BLOCK_COUNT,
    BLOCK_WIDTH,
    FRAME_WIDTH,
    AcceptedFrame,
    ManifestError,
    PilotTraceError,
    TraceFormatError,
    load_manifest_bytes,
    load_trace_bytes,
)

REPORT_SCHEMA_VERSION = 1


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
    """Validate and score exact input bytes without file, device, or network access."""
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
            block_result = {
                "block_index": block_index,
                "channel_range_start_inclusive": block_index * BLOCK_WIDTH,
                "channel_range_end_exclusive": (block_index + 1) * BLOCK_WIDTH,
                "score": score,
                "flagged": is_flagged,
            }
            block_results.append(block_result)
            if is_flagged:
                flagged_block_indices.append(block_index)
                flagged_blocks.append({
                    "frame_index": frame.frame_index,
                    "block_index": block_index,
                    "score": score,
                    "channel_range_start_inclusive": block_index * BLOCK_WIDTH,
                    "channel_range_end_exclusive": (block_index + 1) * BLOCK_WIDTH,
                })
        frame_results.append({
            "frame_index": frame.frame_index,
            "label": frame.label,
            "flagged": bool(flagged_block_indices),
            "flagged_block_indices": flagged_block_indices,
            "blocks": block_results,
        })

    labeled_count = sum(frame.label is not None for frame in accepted)
    rejected_count = len(rejected)
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
            "frames_rejected": rejected_count,
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
    """Read local files as bytes and analyze them using :func:`analyze_bytes`."""
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
    """Serialize a report deterministically as UTF-8-compatible JSON text."""
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


__all__ = [
    "analyze_bytes",
    "analyze_files",
    "report_json",
    "PilotTraceError",
    "TraceFormatError",
    "ManifestError",
    "AcceptedFrame",
    "FRAME_WIDTH",
    "BLOCK_WIDTH",
    "BLOCK_COUNT",
]
