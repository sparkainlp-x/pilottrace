from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from pilottrace import analyze_bytes, report_json, score_block  # noqa: E402
from pilottrace.validation import ManifestError, TraceFormatError  # noqa: E402


METADATA = {
    "schema_version": 1,
    "threshold": 0.5,
    "channel_order": "Caller-declared ordered channels 0 through 511; consecutive groups of 32 define blocks.",
    "units": "dimensionless test units",
    "scaling": "Raw supplied values; no additional scaling is applied.",
    "timebase": "Input frame order only; no elapsed time is asserted.",
    "baseline_preprocessing": "Zero baseline; no preprocessing is applied by this program.",
    "missing_value_policy": "Reject a frame containing a missing or non-finite channel; do not impute.",
    "source_provenance": "Synthetic unit-test fixture authored in this repository.",
    "permitted_use": "Offline unit testing only; no operational use.",
}


def encode(obj: object, *, allow_nan: bool = True) -> bytes:
    return json.dumps(obj, allow_nan=allow_nan, separators=(",", ":")).encode("utf-8")


def trace_bytes(frames: list[object]) -> bytes:
    return encode({"frames": frames})


def frame(value: float = 0.0, label: bool | None = None) -> dict[str, object]:
    result: dict[str, object] = {"values": [value] * 512}
    if label is not None:
        result["label"] = label
    return result


def manifest_bytes(manifest: dict[str, object] | None = None) -> bytes:
    return encode(METADATA if manifest is None else manifest)


def manifest_with_threshold(threshold: object) -> bytes:
    manifest = dict(METADATA)
    manifest["threshold"] = threshold
    return manifest_bytes(manifest)


class ValidationTests(unittest.TestCase):
    def test_wrong_length_is_rejected_and_reported_with_index_and_reason(self) -> None:
        short = {"values": [0.0] * 511}
        report = analyze_bytes(trace_bytes([frame(), short]), manifest_bytes())
        self.assertEqual(report["input_summary"]["frames_accepted"], 1)
        self.assertEqual(report["input_summary"]["frames_rejected"], 1)
        self.assertEqual(report["rejected_frames"][0]["frame_index"], 1)
        self.assertIn("exactly 512", report["rejected_frames"][0]["reasons"][0])

    def test_nan_and_infinity_are_rejected_per_frame(self) -> None:
        values_nan = [0.0] * 512
        values_nan[7] = float("nan")
        values_inf = [0.0] * 512
        values_inf[29] = float("inf")
        report = analyze_bytes(
            trace_bytes([{"values": values_nan}, {"values": values_inf}]),
            manifest_bytes(),
        )
        self.assertEqual(report["input_summary"]["frames_accepted"], 0)
        self.assertEqual(len(report["rejected_frames"]), 2)
        self.assertTrue(any("values[7]" in why and "finite" in why for why in report["rejected_frames"][0]["reasons"]))
        self.assertTrue(any("values[29]" in why and "finite" in why for why in report["rejected_frames"][1]["reasons"]))

    def test_boolean_channel_is_not_a_numeric_real_value(self) -> None:
        values = [0.0] * 512
        values[0] = True
        report = analyze_bytes(trace_bytes([{"values": values}]), manifest_bytes())
        self.assertEqual(report["input_summary"]["frames_rejected"], 1)
        self.assertIn("booleans are not accepted", report["rejected_frames"][0]["reasons"][0])

    def test_missing_manifest_metadata_is_a_contract_error(self) -> None:
        incomplete = dict(METADATA)
        del incomplete["scaling"]
        with self.assertRaisesRegex(ManifestError, "scaling"):
            analyze_bytes(trace_bytes([frame()]), manifest_bytes(incomplete))

    def test_placeholder_manifest_metadata_is_not_explicit(self) -> None:
        incomplete = dict(METADATA)
        incomplete["units"] = "unknown"
        with self.assertRaisesRegex(ManifestError, "explicit"):
            analyze_bytes(trace_bytes([frame()]), manifest_bytes(incomplete))

    def test_malformed_trace_json_is_a_format_error(self) -> None:
        with self.assertRaisesRegex(TraceFormatError, "parse trace JSON"):
            analyze_bytes(b'{"frames": [}', manifest_bytes())

    def test_duplicate_json_keys_are_rejected(self) -> None:
        duplicated = b'{"frames": [], "frames": []}'
        with self.assertRaisesRegex(TraceFormatError, "duplicate JSON object key"):
            analyze_bytes(duplicated, manifest_bytes())

    def test_manifest_threshold_must_be_finite_and_non_negative(self) -> None:
        raw = trace_bytes([frame()])
        for threshold in (-0.1, math.inf, math.nan, True):
            with self.subTest(threshold=threshold):
                with self.assertRaisesRegex(ValueError, "threshold"):
                    analyze_bytes(raw, manifest_with_threshold(threshold))


class ScoringTests(unittest.TestCase):
    def test_known_score_matches_formula(self) -> None:
        values = [0.0] * 31 + [4.0]
        expected = 0.45 * 4.0 + 0.35 * math.sqrt(16.0 / 32.0) + 0.20 * (4.0 / 32.0)
        self.assertAlmostEqual(score_block(values), expected, places=14)

    def test_zero_score_and_extreme_finite_values(self) -> None:
        self.assertEqual(score_block([0.0] * 32), 0.0)
        score = score_block([1.7976931348623157e308] * 32)
        self.assertTrue(math.isfinite(score))
        self.assertEqual(score, 1.7976931348623157e308)

    def test_threshold_is_inclusive_and_block_is_contiguous(self) -> None:
        values = [0.0] * 512
        values[:32] = [1.0] * 32
        report_equal = analyze_bytes(trace_bytes([{"values": values}]), manifest_with_threshold(1.0))
        report_above = analyze_bytes(trace_bytes([{"values": values}]), manifest_with_threshold(1.0000001))
        self.assertEqual(report_equal["flagged_blocks"], [
            {
                "frame_index": 0,
                "block_index": 0,
                "score": 1.0,
                "channel_range_start_inclusive": 0,
                "channel_range_end_exclusive": 32,
            }
        ])
        self.assertEqual(report_above["flagged_blocks"], [])
        self.assertEqual(report_equal["threshold_comparison"], "score >= threshold")

    def test_deterministic_report_and_exact_input_hashes(self) -> None:
        trace = trace_bytes([frame(0.0)])
        manifest = manifest_bytes()
        first = analyze_bytes(trace, manifest)
        second = analyze_bytes(trace, manifest)
        self.assertEqual(first, second)
        self.assertEqual(report_json(first), report_json(second))
        self.assertEqual(first["hashes"]["trace_input_sha256"], hashlib.sha256(trace).hexdigest())
        self.assertEqual(first["hashes"]["manifest_sha256"], hashlib.sha256(manifest).hexdigest())


class ReportingTests(unittest.TestCase):
    def test_labeled_frames_get_explicit_descriptive_classification_metrics(self) -> None:
        quiet_positive = frame(0.0, True)  # false negative
        quiet_negative = frame(0.0, False)  # true negative
        flagged_positive = frame(1.0, True)  # true positive
        flagged_negative = frame(1.0, False)  # false positive
        report = analyze_bytes(
            trace_bytes([quiet_positive, quiet_negative, flagged_positive, flagged_negative]),
            manifest_bytes(),
        )
        metrics = report["labeled_frame_classification"]
        self.assertEqual(metrics["status"], "computed_descriptive_only")
        self.assertEqual(metrics["confusion_counts"], {
            "true_positive": 1, "true_negative": 1, "false_positive": 1, "false_negative": 1
        })
        for name in (
            "descriptive_accuracy", "descriptive_precision", "descriptive_recall_sensitivity",
            "descriptive_specificity", "descriptive_f1_score",
        ):
            self.assertEqual(metrics[name], 0.5)
        self.assertIn("not an estimate", metrics["interpretation"])

    def test_unlabeled_report_does_not_claim_classification_performance(self) -> None:
        report = analyze_bytes(trace_bytes([frame()]), manifest_bytes())
        self.assertEqual(report["labeled_frame_classification"], {
            "status": "not_computed",
            "reason": "No accepted frame carried a boolean label.",
        })

    def test_invalid_labeled_frame_does_not_enter_metrics(self) -> None:
        good = frame(0.0, False)
        bad = {"values": [0.0] * 511, "label": True}
        report = analyze_bytes(trace_bytes([good, bad]), manifest_bytes())
        self.assertEqual(report["labeled_frame_classification"]["labeled_frame_count"], 1)
        self.assertEqual(report["input_summary"]["accepted_unlabeled_frames"], 0)


class CliTests(unittest.TestCase):
    def test_cli_writes_report_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            trace_path = directory / "trace.json"
            manifest_path = directory / "manifest.json"
            output_path = directory / "report.json"
            trace_path.write_bytes(trace_bytes([frame(0.0, False)]))
            manifest_path.write_bytes(manifest_bytes())
            completed = subprocess.run(
                [
                    sys.executable, "-m", "pilottrace", "--trace", str(trace_path),
                    "--manifest", str(manifest_path),
                    "--output", str(output_path),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            parsed = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["input_summary"]["frames_accepted"], 1)
            self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main()
