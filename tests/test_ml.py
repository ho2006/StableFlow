"""Small runnable checks for chronology, missingness, calibration, and CPU weights."""
from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import torch

from stableflow.common import iso
from stableflow.ml import (FlowGRU, Windows, calibrate, group_anomalies, load_model,
                           metrics, prepare, ridge_features, split_boundaries)


def hourly_fixture():
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    manifest = {"start": iso(start), "end": iso(end), "categories": ["dex"], "kind": "synthetic-test"}
    rows = []
    for hour in range(int((end - start).total_seconds() // 3600)):
        flow = 10 + hour % 24
        rows.append({"hour": iso(start + timedelta(hours=hour)), "token": "USDC", "category": "dex",
                     "inflow": float(flow), "outflow": float(flow / 2), "inflow_count": 2, "outflow_count": 1,
                     "active_senders": 2, "active_receivers": 3, "complete": True})
    return rows, manifest


class ForecastingChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.manifest = hourly_fixture()
        cls.prepared = prepare(cls.rows, cls.manifest)
        torch.set_num_threads(1)

    def test_calendar_splits_and_future_perturbation(self):
        bounds = split_boundaries(self.manifest)
        self.assertEqual(bounds["train"]["end"], "2026-07-01T00:00:00Z")
        self.assertEqual(bounds["test"]["start"], "2026-09-01T00:00:00Z")
        annual = split_boundaries({"start": "2025-10-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"})
        self.assertEqual(annual["train"]["end"], "2026-06-01T00:00:00Z")
        self.assertEqual(annual["test"]["start"], "2026-08-01T00:00:00Z")
        changed = copy.deepcopy(self.rows)
        for row in changed:
            if row["hour"] >= "2026-08-01T00:00:00Z":
                row["inflow"] *= 10000
                row["outflow"] *= 10000
        other = prepare(changed, self.manifest)
        self.assertEqual(self.prepared["preprocessing"], other["preprocessing"])
        refs = self.prepared["splits"]["train"][-2:]
        np.testing.assert_array_equal(ridge_features(self.prepared, refs), ridge_features(other, refs))
        validation_refs = self.prepared["splits"]["validation"][-2:]
        np.testing.assert_array_equal(ridge_features(self.prepared, validation_refs), ridge_features(other, validation_refs))
        for i in range(2):
            np.testing.assert_array_equal(Windows(self.prepared, refs)[i][0], Windows(other, refs)[i][0])

    def test_missing_hour_excludes_every_model_window(self):
        rows = copy.deepcopy(self.rows)
        missing = 1000
        rows[missing]["complete"] = False
        rows[missing]["inflow"] = None
        other = prepare(rows, self.manifest)
        target_hours = set(other["refs"][:, 1])
        self.assertFalse(target_hours.intersection(range(missing, missing + 673)))
        self.assertIn(missing + 673, target_hours)
        self.assertEqual(other["missing_targets"], 673)

    def test_lazy_windows_do_not_contain_the_target(self):
        refs = self.prepared["splits"]["train"][:1]
        history, context, target = Windows(self.prepared, refs)[0]
        self.assertEqual(tuple(history.shape), (168, 10))
        self.assertEqual(tuple(context.shape), (10,))
        np.testing.assert_array_equal(history[-1], self.prepared["features"][0, refs[0, 1] - 1])
        np.testing.assert_allclose(target, self.prepared["logs"][0, refs[0, 1], :2], rtol=1e-6)

    def test_checkpoint_weights_roundtrip(self):
        torch.manual_seed(42)
        model = FlowGRU().eval()
        history, context, _ = Windows(self.prepared, self.prepared["splits"]["test"][:1])[0]
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / "weights.pt"
            torch.save(model.state_dict(), checkpoint)
            loaded = load_model(checkpoint)
            with torch.inference_mode():
                self.assertTrue(torch.equal(model(history[None], context[None]), loaded(history[None], context[None])))

    def test_threshold_uses_calibration_and_zero_wape_is_null(self):
        refs = self.prepared["splits"]["calibration"]
        values = self.prepared["values"][refs[:, 0], refs[:, 1], :2].copy()
        calibrated = calibrate(self.prepared, values)
        changed = copy.deepcopy(self.prepared)
        test_refs = changed["splits"]["test"]
        changed["values"][test_refs[:, 0], test_refs[:, 1], :2] *= 1000
        self.assertEqual(calibrated, calibrate(changed, values))
        self.assertEqual(calibrated["USDC", "dex"]["inflow"]["threshold"], 0)
        self.assertIsNone(metrics(np.zeros(3), np.ones(3))["wape"])

    def test_adjacent_flags_are_one_unexplained_event(self):
        rows = []
        for hour in (0, 1, 3):
            rows.append({"hour": f"2026-09-01T0{hour}:00:00Z", "token": "USDC", "category": "dex",
                         "model": "gru", "seed": 42, "split": "test", "anomaly": True, "score": 1.0,
                         "log_residual_inflow": 2.0, "log_residual_outflow": 0.0,
                         "threshold_inflow": 1.0, "threshold_outflow": 1.0,
                         "actual_inflow": 20.0, "actual_outflow": 1.0,
                         "predicted_inflow": 2.0, "predicted_outflow": 1.0})
        events = group_anomalies(rows)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["hours"], 2)
        self.assertEqual(events[0]["end"], "2026-09-01T02:00:00Z")
        self.assertEqual(events[0]["investigation"], "unexplained")


if __name__ == "__main__":
    unittest.main()
