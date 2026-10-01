"""Strict JSON input and manifest validation for PilotTrace."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

FRAME_WIDTH = 512
BLOCK_WIDTH = 32
BLOCK_COUNT = FRAME_WIDTH // BLOCK_WIDTH
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
    """Base class for input or protocol contract errors."""


class TraceFormatError(PilotTraceError):
    """The trace document itself cannot be processed as a trace."""


class ManifestError(PilotTraceError):
    """The manifest is malformed or lacks required metadata."""


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
    # Preserve non-standard JSON numeric tokens so the containing frame can be
    # rejected with its frame index and channel position in the final report.
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
    """Parse a manifest, returning its declarations and frozen score threshold."""
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

    allowed = {"values", "label"}
    extras = sorted(set(obj) - allowed)
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
            reasons.append(f"{where}.values must contain exactly {FRAME_WIDTH} values (got {len(values)})")
        for channel_index, value in enumerate(values):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                reasons.append(f"{where}.values[{channel_index}] must be a finite numeric real value (booleans are not accepted)")
                continue
            try:
                converted = float(value)
            except (OverflowError, ValueError):
                reasons.append(f"{where}.values[{channel_index}] is outside the supported finite numeric range")
                continue
            if not math.isfinite(converted):
                reasons.append(f"{where}.values[{channel_index}] must be finite (NaN and infinity are not accepted)")
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
    """Parse a trace and return accepted frames, rejected-frame details, and count."""
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
