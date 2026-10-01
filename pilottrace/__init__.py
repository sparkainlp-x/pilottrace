"""PilotTrace: a small, offline, illustrative trace-scoring prototype."""

from .api import analyze_bytes, analyze_files, report_json
from .scoring import score_block, score_frame
from .validation import ManifestError, PilotTraceError, TraceFormatError

__version__ = "0.1.0"

__all__ = [
    "analyze_bytes",
    "analyze_files",
    "report_json",
    "score_block",
    "score_frame",
    "PilotTraceError",
    "ManifestError",
    "TraceFormatError",
]
