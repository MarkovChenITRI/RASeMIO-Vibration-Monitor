from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app.analysis import (ProcessingSettings, analyze_recording, diagnose_range, frequency_analysis,
                          iso10816_reference, metrics_for_range, resolve_sensor_timeline, status_mask_intervals,
                          stft_analysis)
from app.main import App
from app.storage import ObservationStore


class ObservationStoreTests(unittest.TestCase):
    def test_history_persists_and_queries_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.sqlite3"
            with ObservationStore(path) as store:
                store.append_many([("2026-09-09T02:00:00.000000Z", 1), ("2026-09-09T02:00:00.200000Z", 2)])
                page = store.query(limit=10, modes={2})
                self.assertEqual([(row.timestamp, row.mode) for row in page.rows], [("2026-09-09T02:00:00.200000Z", 2)])
                self.assertEqual(page.total, 1)
            with ObservationStore(path) as reopened:
                self.assertEqual(reopened.query(limit=10).total, 2)

    def test_available_hours_only_returns_hours_containing_data(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with ObservationStore(Path(folder) / "history.sqlite3") as store:
                store.append_many([
                    ("2026-09-09T02:10:00.000000Z", 1),
                    ("2026-09-09T02:50:00.000000Z", 2),
                    ("2026-09-09T05:00:00.000000Z", 3),
                ])
                self.assertEqual(store.available_hours(), ("2026-09-09T02", "2026-09-09T05"))

    def test_extended_rasemio_snapshot_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with ObservationStore(Path(folder) / "history.sqlite3") as store:
                store.append_many([{
                    "timestamp": "2026-09-09T02:00:00.000000Z", "mode": 1,
                    "motion_status": 0, "selected_gripper": 1,
                    "ptp_speed_pct": 50.0, "linear_speed_mm_s": 120.0,
                    "speed_ratio_pct": 80.0, "acceleration_ms": 200.0,
                    "deceleration_ms": 250.0,
                    "commanded_joints": "[1.0,2.0]", "encoder_joints": "[1.1,2.1]",
                    "commanded_position": "[10.0,20.0,30.0]",
                }])
                row = store.query(limit=1).rows[0]
                self.assertEqual(row.motion_status, 0)
                self.assertEqual(row.encoder_joints, "[1.1,2.1]")

    def test_history_values_are_normalized_and_paginated(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with ObservationStore(Path(folder) / "history.sqlite3") as store:
                store.append_many([{
                    "timestamp": "2026-09-09T02:00:00.000000Z", "mode": 1,
                    "motion_status": 0, "selected_gripper": None,
                }])
                page = store.query_values(fields=("mode", "motion_status", "selected_gripper"), limit=10)
                self.assertEqual(page.total, 2)
                self.assertEqual(
                    {(row.field, row.value) for row in page.rows},
                    {("mode", "1"), ("motion_status", "0")},
                )
                single = store.query_values(fields=("mode",), limit=1)
                self.assertEqual(single.total, 1)
                self.assertEqual(single.rows[0].field, "mode")

    def test_existing_mode_only_database_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.sqlite3"
            import sqlite3
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, mode INTEGER NOT NULL)")
            connection.execute("INSERT INTO observations(timestamp,mode) VALUES ('2026-09-09T02:00:00Z',2)")
            connection.commit(); connection.close()
            with ObservationStore(path) as store:
                row = store.query(limit=1).rows[0]
                self.assertEqual(row.mode, 2)
                self.assertIsNone(row.motion_status)


class AnalysisServiceTests(unittest.TestCase):
    def test_diagnosis_reports_events_and_cautious_evidence(self) -> None:
        aligned = pd.DataFrame({
            "time_s": [0.0, .1, .2], "motion_status": [0, 0, 1],
            "selected_gripper": [1, 1, 1], "commanded_joints": ["[0]", "[1]", "[1]"],
            "encoder_joints": ["[0]", "[.9]", "[1]"], "commanded_position": ["[0]", "[1]", "[1]"],
            "mode": [1, 1, 1],
        })
        processed = pd.DataFrame({
            "time_s": [0.0, .1, .2], "segment": [0, 0, 0],
            "X": [0.0, .4, .4], "Y": [0.0, 0.0, 0.0], "Z": [0.0, 0.0, 0.0],
        })
        result = diagnose_range(aligned, processed, 0, .2, 10, .3, 2)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events.iloc[0].axis, "X")
        self.assertEqual([row[0] for row in result.evidence], ["路徑／動作規劃", "驅動參數", "機構"])

    def test_visible_metrics_focus_on_acceptance_and_diagnosis(self) -> None:
        keys = [row[2] for row in App.metric_rows()]
        self.assertEqual(
            keys,
            [
                "threshold_g", "analyzed_seconds", "RMS_g", "Max_abs_g",
                "P99_abs_g", "P99.9_abs_g", "verdict", "limit_margin_g", "exceed",
                "exceed_pct", "event_count", "events_per_min",
                "max_event_ms", "total_excursion_ms",
            ],
        )
        self.assertNotIn("median_abs_g", keys)
        self.assertNotIn("crest_factor", keys)
        self.assertNotIn("peak_to_peak_g", keys)

    def test_limit_margin_reports_distance_from_maximum_to_threshold(self) -> None:
        processed = pd.DataFrame({
            "time_s": [0.0, 0.01], "segment": [0, 0],
            "X": [0.1, 0.4], "Y": [0.1, 0.2], "Z": [0.0, 0.0],
        })
        metrics = metrics_for_range(processed, 0, 1, 100, .3)
        self.assertAlmostEqual(float(metrics.loc["X", "limit_margin_g"]), -.1)
        self.assertAlmostEqual(float(metrics.loc["Y", "limit_margin_g"]), .1)

    def make_sensor(self) -> pd.DataFrame:
        return pd.DataFrame({
            "time_s": np.arange(0, 2, 0.01),
            "X": np.r_[np.full(150, 0.1), np.full(50, 0.4)],
            "Y": np.zeros(200),
            "Z": np.zeros(200),
        })

    def test_non_exact_timestamps_and_stale_gaps_control_selection(self) -> None:
        start = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
        observations = pd.DataFrame({
            "timestamp": [start + timedelta(seconds=.03), start + timedelta(seconds=.23), start + timedelta(seconds=1.23)],
            "mode": [1, 1, 2],
        })
        result = analyze_recording(self.make_sensor(), start, observations, {1}, ProcessingSettings(20, 100), .2)
        self.assertTrue(result.has_overlap)
        self.assertGreater(result.covered_samples, 0)
        self.assertGreater(result.unknown_samples, 0)
        self.assertTrue(set(result.aligned.loc[result.aligned.selected, "mode"].dropna()) <= {1})

    def test_extended_status_filters_use_or_within_field_and_and_between_fields(self) -> None:
        start = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
        observations = pd.DataFrame({
            "timestamp": [start + timedelta(seconds=.2 * index) for index in range(10)],
            "mode": [1, 2] * 5,
            "motion_status": [0, 0, 1, 1, 0, 0, 1, 1, 0, 0],
            "ptp_speed_pct": [10, 20, 20, 30, 25, 40, 20, 20, 30, 25],
        })
        filters = {
            "mode": {"values": {1, 2}},
            "motion_status": {"values": {0}},
            "ptp_speed_pct": {"minimum": 20, "maximum": 30},
        }
        result = analyze_recording(self.make_sensor(), start, observations, set(), ProcessingSettings(20, 100), .2, filters)
        selected = result.aligned.loc[result.aligned.selected]
        self.assertFalse(selected.empty)
        self.assertTrue(selected["mode"].isin({1, 2}).all())
        self.assertTrue(selected["motion_status"].eq(0).all())
        self.assertTrue(selected["ptp_speed_pct"].between(20, 30).all())

    def test_status_offset_shifts_which_sensor_samples_are_selected(self) -> None:
        start = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
        observations = pd.DataFrame({
            "timestamp": [start + timedelta(seconds=.2 * index) for index in range(10)],
            "mode": [0] * 5 + [1] * 5,
        })
        filters = {"mode": {"values": {1}}}
        unshifted = analyze_recording(self.make_sensor(), start, observations, set(), ProcessingSettings(20, 100), .2, filters)
        shifted = analyze_recording(self.make_sensor(), start, observations, set(), ProcessingSettings(20, 100), .2, filters, .5)
        self.assertAlmostEqual(float(unshifted.aligned.loc[unshifted.aligned.selected, "time_s"].min()), 1.0)
        self.assertAlmostEqual(float(shifted.aligned.loc[shifted.aligned.selected, "time_s"].min()), 1.5)

    def test_status_mask_intervals_merge_runs_apply_offset_and_break_on_stale_gaps(self) -> None:
        start = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
        observations = pd.DataFrame({
            "timestamp": [start + timedelta(seconds=value) for value in (0, .2, .4, .6, .8, 3.0)],
            "mode": [0, 0, 0, 1, 1, 1],
        })
        intervals = status_mask_intervals(observations, start, .2, .1)
        expected = [(.1, .7, 0), (.7, 1.5, 1), (3.1, 3.7, 1)]
        self.assertEqual(len(intervals), len(expected))
        for (begin, end, value), (e_begin, e_end, e_value) in zip(intervals, expected):
            self.assertAlmostEqual(begin, e_begin); self.assertAlmostEqual(end, e_end); self.assertEqual(value, e_value)

    def test_metrics_expose_threshold_numerator_and_denominator(self) -> None:
        start = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
        observations = pd.DataFrame({"timestamp": [start], "mode": [1]})
        sensor = self.make_sensor(); sensor["X"] = 0.4
        result = analyze_recording(sensor, start, observations, set(), ProcessingSettings(20, 100), 1.0)
        x = result.metrics.loc["X"]
        self.assertEqual(int(x["total_samples"]), 200)
        self.assertEqual(int(x["exceed_samples"]), 200)
        self.assertAlmostEqual(x["exceed_pct"], 100.0)

    def test_metrics_for_interactive_range_use_only_selected_times(self) -> None:
        processed = pd.DataFrame({
            "time_s": [0.0, 0.1, 0.2, 0.3], "segment": [0, 0, 0, 0],
            "X": [0.1, 0.1, 0.4, 0.4], "Y": [0.0] * 4, "Z": [0.0] * 4,
        })
        metrics = metrics_for_range(processed, 0.15, 0.35, 10)
        self.assertEqual(int(metrics.loc["X", "total_samples"]), 2)
        self.assertEqual(int(metrics.loc["X", "exceed_samples"]), 2)

    def test_debounce_filters_events_but_not_exceed_samples(self) -> None:
        processed = pd.DataFrame({
            "time_s": np.arange(9) / 100,
            "segment": [0] * 9,
            "X": [0, .3, 0, .4, .4, 0, .5, .5, .5],
            "Y": [0] * 9,
            "Z": [0] * 9,
        })
        metrics = metrics_for_range(processed, 0, 1, 100, .3, 3)
        x = metrics.loc["X"]
        self.assertEqual(int(x.exceed_samples), 6)
        self.assertEqual(int(x.event_count), 1)
        self.assertAlmostEqual(x.max_event_ms, 30.0)
        self.assertAlmostEqual(x.total_excursion_ms, 30.0)

    def test_peak_to_peak_is_full_signed_range(self) -> None:
        processed = pd.DataFrame({
            "time_s": [0.0, 0.01, 0.02], "segment": [0, 0, 0],
            "X": [-.4, 0, .6], "Y": [0, 0, 0], "Z": [0, 0, 0],
        })
        metrics = metrics_for_range(processed, 0, 1, 100)
        self.assertAlmostEqual(metrics.loc["X", "peak_to_peak_g"], 1.0)

    def test_frequency_analysis_finds_known_tone_and_band(self) -> None:
        hz = 512
        time_s = np.arange(0, 4, 1 / hz)
        tone = np.sin(2 * np.pi * 25 * time_s)
        processed = pd.DataFrame({"time_s": time_s, "segment": 0, "X": tone, "Y": tone, "Z": tone})
        result = frequency_analysis(processed, 0, 4, hz, 200)
        x_peak = result.peaks.query("axis == 'X' and rank == 1").iloc[0]
        self.assertAlmostEqual(float(x_peak.frequency_hz), 25.0, places=1)
        middle = result.bands.query("axis == 'X' and band == '中頻'").iloc[0]
        self.assertGreater(float(middle.power_pct), 99.0)

    def test_custom_stft_finds_tone_in_requested_band(self) -> None:
        hz=512; time_s=np.arange(0,4,1/hz); tone=np.sin(2*np.pi*25*time_s)
        processed=pd.DataFrame({"time_s":time_s,"segment":0,"X":tone,"Y":tone,"Z":tone})
        result=stft_analysis(processed,0,4,hz,"X",10,50,1.0,75)
        self.assertTrue(result.frequency_hz.min()>=10)
        self.assertTrue(result.frequency_hz.max()<=50)
        self.assertAlmostEqual(float(result.hotspots.iloc[0].frequency_hz),25.0,places=1)

    def test_iso10816_reference_uses_user_boundaries(self) -> None:
        hz=512; time_s=np.arange(0,4,1/hz); tone=.1*np.sin(2*np.pi*25*time_s)
        processed=pd.DataFrame({"time_s":time_s,"segment":0,"X":tone,"Y":tone,"Z":tone})
        result=iso10816_reference(processed,0,4,hz,10,100,(.1,.2,.3))
        self.assertGreater(float(result.loc["X","velocity_rms_mm_s"]),0)
        self.assertIn(result.loc["X","zone"],tuple("ABCD"))

    def test_configurable_acceptance_threshold_changes_exceedance(self) -> None:
        start = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
        observations = pd.DataFrame({"timestamp": [start], "mode": [1]})
        sensor = self.make_sensor(); sensor["X"] = 0.4
        result = analyze_recording(sensor, start, observations, set(),
                                   ProcessingSettings(20, 100, 0.5), 1.0)
        self.assertEqual(int(result.metrics.loc["X", "exceed_samples"]), 0)

    def test_sensor_timeline_uses_local_start(self) -> None:
        start = datetime(2026, 9, 9, 10, 0).astimezone()
        timeline = resolve_sensor_timeline(pd.Series([0.0, .5]), start)
        self.assertEqual(len(timeline), 2)
        self.assertEqual((timeline.iloc[1] - timeline.iloc[0]).total_seconds(), .5)

    def test_zero_overlap_ignores_mode_filter_and_analyzes_full_recording(self) -> None:
        start = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
        result = analyze_recording(
            self.make_sensor(), start, pd.DataFrame(columns=["timestamp", "mode"]),
            {1}, ProcessingSettings(20, 100), .2,
        )
        self.assertFalse(result.has_overlap)
        self.assertEqual(result.selected_samples, result.total_samples)
        self.assertEqual(int(result.metrics.loc["X", "total_samples"]), 200)


if __name__ == "__main__":
    unittest.main()
