"""Scenario tests for the independent manifest replay."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from implementation import replay


ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec"


def base_manifest(**overrides: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "manifest_version": 1,
        "symbol": "TEST",
        "direction": "long",
        "input_file": "bars.csv",
        "input_columns": ["index", "timestamp_utc", "close"],
        "fast_window": 2,
        "slow_window": 3,
        "mean_round_decimal_places": 9,
        "signal_rule": replay.SIGNAL_RULE,
        "edge_rule": replay.EDGE_RULE,
        "minimum_signal_index": 3,
        "decision_offset_seconds": 1,
        "execution_rule": replay.EXECUTION_RULE,
        "maximum_outputs": 25,
        "output_file": "oracle-output.json",
        "output_schema": "output-schema.json",
    }
    manifest.update(overrides)
    return manifest


class ReplayScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.spec = Path(self.temporary.name)
        (self.spec / "output-schema.json").write_bytes((SPEC / "output-schema.json").read_bytes())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_fixture(self, closes: list[float], manifest: dict[str, object] | None = None) -> None:
        (self.spec / "manifest.json").write_text(json.dumps(manifest or base_manifest()), encoding="utf-8")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with (self.spec / "bars.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["index", "timestamp_utc", "close"])
            for index, close in enumerate(closes):
                writer.writerow([index, (start + timedelta(minutes=index)).isoformat(), close])

    def test_edge_persona_gets_only_false_to_true_transitions(self) -> None:
        """A trader scanning a sustained pullback receives one alert per entry, not per bar."""
        self.write_fixture([1, 2, 4, 3, 2.5, 5, 4])
        output, *_ = replay.build_output(self.spec)
        self.assertEqual([signal["index"] for signal in output["signals"]], [3, 6])

    def test_window_persona_waits_for_history_and_minimum_index(self) -> None:
        """A cold-start operator cannot emit from a partial slow window or before the gate."""
        self.write_fixture([1, 2, 4, 3], base_manifest(minimum_signal_index=4))
        output, *_ = replay.build_output(self.spec)
        self.assertEqual(output["signals"], [])

    def test_capacity_persona_sees_deterministic_earliest_truncation(self) -> None:
        """A bounded consumer gets the first two edge events when later events exceed capacity."""
        self.write_fixture([1, 2, 4, 3, 5, 4, 6, 5, 7, 6], base_manifest(maximum_outputs=2))
        output, *_ = replay.build_output(self.spec)
        self.assertEqual([signal["index"] for signal in output["signals"]], [3, 5])

    def test_ingestion_persona_rejects_malformed_nonsequential_rows(self) -> None:
        """An operator with a corrupt export gets a hard error instead of plausible-looking output."""
        (self.spec / "manifest.json").write_text(json.dumps(base_manifest()), encoding="utf-8")
        (self.spec / "bars.csv").write_text(
            "index,timestamp_utc,close\n0,2026-01-01T00:00:00+00:00,1\n2,2026-01-01T00:01:00+00:00,2\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(replay.ReplayError, "sequential index"):
            replay.build_output(self.spec)

    def test_auditor_replays_sustained_full_corpus_twice_identically(self) -> None:
        """An auditor processing all 2,400 bars sees bounded, schema-valid, repeatable results."""
        first, *_ = replay.build_output(SPEC)
        second, *_ = replay.build_output(SPEC)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first["signals"]), 25)
        replay.validate_schema(first, replay.load_json(SPEC / "output-schema.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
