from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pilottrace_standalone as standalone  # noqa: E402
from pilottrace import analyze_bytes as modular_analyze_bytes  # noqa: E402


METADATA = {
    "schema_version": 1,
    "threshold": 0.5,
    "channel_order": "Caller-declared ordered values 0 through 511, grouped into blocks of 32.",
    "units": "dimensionless test units",
    "scaling": "Values are used as supplied without additional scaling.",
    "timebase": "Input frame order only; no elapsed time is asserted.",
    "baseline_preprocessing": "Zero baseline; no preprocessing is applied by this program.",
    "missing_value_policy": "Reject a frame containing missing or non-finite values; do not impute.",
    "source_provenance": "Synthetic local test fixture.",
    "permitted_use": "Offline unit testing only; not operational use.",
}


def encode(obj: object, *, allow_nan: bool = True) -> bytes:
    return json.dumps(obj, allow_nan=allow_nan, separators=(",", ":")).encode("utf-8")


def trace_bytes(frames: list[object]) -> bytes:
    return encode({"frames": frames})


def manifest_bytes(manifest: dict[str, object] | None = None) -> bytes:
    return encode(METADATA if manifest is None else manifest)


def frame(value: float = 0.0, label: bool | None = None) -> dict[str, object]:
    item: dict[str, object] = {"values": [value] * 512}
    if label is not None:
        item["label"] = label
    return item


class StandaloneValidationTests(unittest.TestCase):
    def test_rejected_frames_include_zero_based_index_and_detailed_reasons(self) -> None:
        nonfinite = [0.0] * 512
        nonfinite[17] = math.inf
        invalid = [
            {"values": [0.0] * 511},
            {"values": nonfinite},
            {"values": [True] + [0.0] * 511},
        ]
        report = standalone.analyze_bytes(
            trace_bytes([frame(), *invalid]), manifest_bytes()
        )
        self.assertEqual(report["input_summary"]["frames_accepted"], 1)
        self.assertEqual(report["input_summary"]["frames_rejected"], 3)
        self.assertEqual([item["frame_index"] for item in report["rejected_frames"]], [1, 2, 3])
        self.assertIn("exactly 512", report["rejected_frames"][0]["reasons"][0])
        self.assertTrue(any("values[17]" in reason and "finite" in reason
                            for reason in report["rejected_frames"][1]["reasons"]))
        self.assertTrue(any("booleans are not accepted" in reason
                            for reason in report["rejected_frames"][2]["reasons"]))

    def test_manifest_is_strict_and_threshold_is_finite_nonnegative(self) -> None:
        trace = trace_bytes([frame()])
        for threshold in (-0.1, math.inf, math.nan, True):
            with self.subTest(threshold=threshold):
                bad_manifest = dict(METADATA)
                bad_manifest["threshold"] = threshold
                with self.assertRaisesRegex(standalone.ManifestError, "threshold"):
                    standalone.analyze_bytes(trace, manifest_bytes(bad_manifest))
        bad_manifest = dict(METADATA)
        bad_manifest["extra"] = "not permitted"
        with self.assertRaisesRegex(standalone.ManifestError, "unknown field"):
            standalone.analyze_bytes(trace, manifest_bytes(bad_manifest))
        bad_manifest = dict(METADATA)
        del bad_manifest["scaling"]
        with self.assertRaisesRegex(standalone.ManifestError, "scaling"):
            standalone.analyze_bytes(trace, manifest_bytes(bad_manifest))

    def test_duplicate_keys_and_bad_trace_roots_are_rejected(self) -> None:
        with self.assertRaisesRegex(standalone.TraceFormatError, "duplicate JSON object key"):
            standalone.analyze_bytes(b'{"frames":[],"frames":[]}', manifest_bytes())
        with self.assertRaisesRegex(standalone.TraceFormatError, "must contain at least one"):
            standalone.analyze_bytes(b'{"frames":[]}', manifest_bytes())


class StandaloneScoringAndReportTests(unittest.TestCase):
    def test_formula_and_16_contiguous_blocks(self) -> None:
        values = [0.0] * 512
        values[31] = 4.0
        expected = 0.45 * 4.0 + 0.35 * math.sqrt(16.0 / 32.0) + 0.20 * (4.0 / 32.0)
        self.assertAlmostEqual(standalone.score_block(values[:32]), expected, places=14)
        scores = standalone.score_frame(values)
        self.assertEqual(len(scores), 16)
        self.assertAlmostEqual(scores[0], expected, places=14)
        self.assertEqual(scores[1:], [0.0] * 15)

    def test_frozen_threshold_is_inclusive_and_report_hashes_are_exact(self) -> None:
        values = [1.0] * 32 + [0.0] * (512 - 32)
        manifest = dict(METADATA)
        manifest["threshold"] = 1.0
        raw_manifest = manifest_bytes(manifest)
        raw_trace = trace_bytes([{"values": values}])
        report = standalone.analyze_bytes(raw_trace, raw_manifest)
        self.assertEqual(report["threshold_comparison"], "score >= threshold")
        self.assertEqual(report["flagged_blocks"][0]["block_index"], 0)
        self.assertEqual(report["flagged_blocks"][0]["channel_range_start_inclusive"], 0)
        self.assertEqual(report["flagged_blocks"][0]["channel_range_end_exclusive"], 32)
        self.assertEqual(report["hashes"]["trace_input_sha256"], hashlib.sha256(raw_trace).hexdigest())
        self.assertEqual(report["hashes"]["manifest_sha256"], hashlib.sha256(raw_manifest).hexdigest())
        self.assertEqual(standalone.report_json(report), standalone.report_json(
            standalone.analyze_bytes(raw_trace, raw_manifest)
        ))

    def test_metrics_are_computed_only_for_supplied_labels(self) -> None:
        unlabeled = standalone.analyze_bytes(trace_bytes([frame()]), manifest_bytes())
        self.assertEqual(unlabeled["labeled_frame_classification"], {
            "status": "not_computed",
            "reason": "No accepted frame carried a boolean label.",
        })
        labeled = standalone.analyze_bytes(
            trace_bytes([frame(0.0, False), frame(1.0, True)]), manifest_bytes()
        )
        metrics = labeled["labeled_frame_classification"]
        self.assertEqual(metrics["status"], "computed_descriptive_only")
        self.assertEqual(metrics["labeled_frame_count"], 2)
        self.assertEqual(metrics["confusion_counts"]["true_negative"], 1)
        self.assertEqual(metrics["confusion_counts"]["true_positive"], 1)

    def test_standalone_and_modular_reports_match(self) -> None:
        raw_trace = trace_bytes([frame(0.0, False), frame(1.0, True), {"values": [0.0]}])
        raw_manifest = manifest_bytes()
        self.assertEqual(
            standalone.analyze_bytes(raw_trace, raw_manifest),
            modular_analyze_bytes(raw_trace, raw_manifest),
        )


class StandaloneCliTests(unittest.TestCase):
    def test_cli_runs_from_a_copy_without_the_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            script_path = directory / "pilottrace_standalone.py"
            shutil.copy2(PROJECT_ROOT / "pilottrace_standalone.py", script_path)
            trace_path = directory / "trace.json"
            manifest_path = directory / "manifest.json"
            output_path = directory / "report.json"
            trace_path.write_bytes(trace_bytes([frame(0.0, False), frame(1.0, True)]))
            manifest_path.write_bytes(manifest_bytes())
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["PYTHONNOUSERSITE"] = "1"
            completed = subprocess.run(
                [sys.executable, str(script_path), "--trace", str(trace_path),
                 "--manifest", str(manifest_path), "--output", str(output_path)],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["input_summary"]["frames_accepted"], 2)
            self.assertEqual(len(report["frames"][0]["blocks"]), 16)
            self.assertEqual(report["labeled_frame_classification"]["status"],
                             "computed_descriptive_only")
            self.assertEqual(report["scope_note"],
                             "Offline processing of caller-mapped numeric data only. This "
                             "prototype is not validated operational detection and does not "
                             "provide alerts or control functions.")

    def test_invalid_manifest_returns_nonzero_without_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            trace_path = directory / "trace.json"
            manifest_path = directory / "manifest.json"
            trace_path.write_bytes(trace_bytes([frame()]))
            manifest = dict(METADATA)
            del manifest["units"]
            manifest_path.write_bytes(manifest_bytes(manifest))
            completed = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "pilottrace_standalone.py"),
                 "--trace", str(trace_path), "--manifest", str(manifest_path)],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, "")
            self.assertIn("manifest is missing", completed.stderr)


if __name__ == "__main__":
    unittest.main()
