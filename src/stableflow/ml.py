"""CPU forecasting and chronological residual calibration for stablecoin flows.

Windows are sliced lazily from a small hourly array; no N x 168 copy is made.
Forecasts are research backtests, and residual flags are not anomaly ground truth.
"""
from __future__ import annotations

import copy
import math
import os
import platform
import random
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import sklearn
import torch
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from torch import nn
from torch.utils.data import DataLoader, Dataset

from .common import CATEGORIES, NUMERIC_FEATURES, TOKENS, iso, utc, write_json, write_parquet

WARMUP = 4 * 7 * 24
BASELINES = ("lag24", "lag168", "weekly_median", "ridge")


def _datetime(value) -> datetime:
    return utc(value.isoformat() if isinstance(value, datetime) else str(value))


def _month(value: datetime, offset: int) -> datetime:
    total = value.year * 12 + value.month - 1 + offset
    return value.replace(year=total // 12, month=total % 12 + 1)


def split_boundaries(manifest: dict) -> dict:
    """Whole calendar months, with disjoint target-hour splits."""
    start, end = _datetime(manifest["start"]), _datetime(manifest["end"])
    if any(d.day != 1 or d.hour or d.minute or d.second or d.microsecond for d in (start, end)):
        raise ValueError("Research interval must contain complete UTC calendar months")
    months = (end.year - start.year) * 12 + end.month - start.month
    if months not in (6, 12):
        raise ValueError("Forecasting requires exactly six or twelve complete calendar months")
    train_months = 8 if months == 12 else 3
    points = [start + timedelta(hours=WARMUP), _month(start, train_months),
              _month(start, train_months + 1), _month(start, train_months + 2), end]
    return {name: {"start": iso(points[i]), "end": iso(points[i + 1])}
            for i, name in enumerate(("train", "validation", "calibration", "test"))}


def prepare(rows: list[dict], manifest: dict, lookback: int = 168) -> dict:
    """Validate inputs and select identical complete-history targets for every model."""
    if not isinstance(lookback, int) or not 1 <= lookback <= WARMUP:
        raise ValueError(f"lookback must be an integer from 1 to {WARMUP}")
    boundaries = split_boundaries(manifest)
    start, end = _datetime(manifest["start"]), _datetime(manifest["end"])
    hours = int((end - start).total_seconds() // 3600)
    covered = set(manifest.get("categories", CATEGORIES)).intersection(CATEGORIES)
    keys = sorted({(r["token"], r["category"]) for r in rows
                   if r["category"] in covered and r["token"] in TOKENS})
    if not keys:
        raise ValueError("No covered, verified category has hourly data")
    key_index = {key: i for i, key in enumerate(keys)}
    values = np.zeros((len(keys), hours, len(NUMERIC_FEATURES)), dtype=np.float64)
    valid = np.zeros((len(keys), hours), dtype=bool)
    seen = set()
    for row in rows:
        key = row["token"], row["category"]
        if key not in key_index:
            continue
        timestamp = _datetime(row["hour"])
        offset = (timestamp - start).total_seconds() / 3600
        if not offset.is_integer() or not 0 <= offset < hours:
            raise ValueError("Hourly row is unaligned or outside the snapshot interval")
        series, hour = key_index[key], int(offset)
        if (series, hour) in seen:
            raise ValueError("Duplicate hourly series row")
        seen.add((series, hour))
        if not row.get("complete", False):
            continue
        try:
            numeric = np.asarray([row[field] for field in NUMERIC_FEATURES], dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise ValueError("Complete hourly row has missing numeric features") from exc
        if not np.isfinite(numeric).all() or np.any(numeric < 0):
            raise ValueError("Complete features must be finite and nonnegative")
        if np.any(numeric[2:] != np.floor(numeric[2:])):
            raise ValueError("Activity and transaction counts must be integers")
        values[series, hour] = numeric
        valid[series, hour] = True
    times = [start + timedelta(hours=i) for i in range(hours)]
    cycles = np.asarray([[math.sin(2 * math.pi * t.hour / 24), math.cos(2 * math.pi * t.hour / 24),
                          math.sin(2 * math.pi * t.weekday() / 7), math.cos(2 * math.pi * t.weekday() / 7)]
                         for t in times], dtype=np.float32)
    # An absent hour excludes every model's target and its preceding 672-hour history.
    invalid_prefix = np.pad(np.cumsum(~valid, axis=1), ((0, 0), (1, 0)))
    candidate_hours = np.arange(WARMUP, hours)
    eligible = valid[:, WARMUP:] & ((invalid_prefix[:, candidate_hours] -
                                    invalid_prefix[:, candidate_hours - WARMUP]) == 0)
    refs = np.argwhere(eligible)
    refs[:, 1] += WARMUP
    splits = {}
    for name, bounds in boundaries.items():
        lo = int((_datetime(bounds["start"]) - start).total_seconds() // 3600)
        hi = int((_datetime(bounds["end"]) - start).total_seconds() // 3600)
        splits[name] = refs[(refs[:, 1] >= lo) & (refs[:, 1] < hi)]
        if not len(splits[name]):
            raise ValueError(f"No common complete windows in {name}; repair source coverage")
    train_end = int((_datetime(boundaries["train"]["end"]) - start).total_seconds() // 3600)
    logs = np.log1p(values)
    train_values = logs[:, :train_end][valid[:, :train_end]]
    mean, scale = train_values.mean(axis=0), train_values.std(axis=0)
    scale = np.where(scale < 1e-8, 1.0, scale)
    normalized = ((logs - mean) / scale).astype(np.float32)
    expanded_cycles = np.broadcast_to(cycles, (len(keys), hours, 4))
    features = np.concatenate((normalized, expanded_cycles), axis=2)
    identities = np.zeros((len(keys), len(TOKENS) + len(CATEGORIES)), dtype=np.float32)
    for i, (token, category) in enumerate(keys):
        identities[i, list(TOKENS).index(token)] = 1
        identities[i, len(TOKENS) + list(CATEGORIES).index(category)] = 1
    return {"keys": keys, "values": values, "logs": logs, "features": features,
            "cycles": cycles, "identities": identities, "refs": refs, "splits": splits,
            "boundaries": boundaries, "times": times, "lookback": lookback,
            "preprocessing": {"numeric_features": list(NUMERIC_FEATURES), "log1p": True,
                              "mean": mean.tolist(), "scale": scale.tolist(),
                              "fit_before": boundaries["train"]["end"],
                              "identity_tokens": list(TOKENS), "identity_categories": list(CATEGORIES)},
            "missing_targets": int(len(keys) * (hours - WARMUP) - len(refs))}


class Windows(Dataset):
    def __init__(self, prepared: dict, refs: np.ndarray, flow_only: bool = False):
        self.prepared, self.refs = prepared, refs
        self.features = prepared["features"] if not flow_only else prepared["features"][:, :, [0, 1, 6, 7, 8, 9]]

    def __len__(self):
        return len(self.refs)

    def __getitem__(self, index):
        series, hour = self.refs[index]
        context = np.concatenate((self.prepared["identities"][series], self.prepared["cycles"][hour]))
        return (torch.from_numpy(self.features[series, hour - self.prepared["lookback"]:hour]),
                torch.from_numpy(context), torch.from_numpy(self.prepared["logs"][series, hour, :2].astype(np.float32)))


class FlowGRU(nn.Module):
    def __init__(self, input_size: int = 10, context_size: int = 10):
        super().__init__()
        self.gru = nn.GRU(input_size, 32, num_layers=1, batch_first=True)
        self.output = nn.Linear(32 + context_size, 2)

    def forward(self, history, context):
        _, hidden = self.gru(history)
        return self.output(torch.cat((hidden[-1], context), dim=1))


def load_model(path: Path, *, flow_only: bool = False) -> FlowGRU:
    """Load only tensor weights; never unpickle arbitrary checkpoint objects."""
    model = FlowGRU(input_size=6 if flow_only else 10)
    model.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
    return model.eval()


def _predict_logs(model: FlowGRU, dataset: Windows) -> np.ndarray:
    model.eval()
    parts = []
    with torch.inference_mode():
        for history, context, _ in DataLoader(dataset, batch_size=512, shuffle=False, num_workers=0):
            result = model(history, context)
            if not torch.isfinite(result).all():
                raise ValueError("Model produced non-finite log predictions")
            parts.append(result.numpy().astype(np.float64))
    return np.concatenate(parts)


def _original(log_predictions: np.ndarray) -> np.ndarray:
    with np.errstate(over="raise", invalid="raise"):
        try:
            result = np.maximum(0, np.expm1(log_predictions))
        except FloatingPointError as exc:
            raise ValueError("Model inverse transformation overflowed") from exc
    if not np.isfinite(result).all():
        raise ValueError("Model produced non-finite predictions")
    return result


def _train(prepared: dict, seed: int, epochs: int, flow_only: bool, output: Path):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    model = FlowGRU(input_size=6 if flow_only else 10)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.HuberLoss()
    train = Windows(prepared, prepared["splits"]["train"], flow_only)
    validation = Windows(prepared, prepared["splits"]["validation"], flow_only)
    loader = DataLoader(train, batch_size=256, shuffle=True, num_workers=0,
                        generator=torch.Generator().manual_seed(seed))
    best, best_state, stale, curve = float("inf"), None, 0, []
    for epoch in range(epochs):
        model.train()
        total = 0.0
        for history, context, targets in loader:
            optimizer.zero_grad(set_to_none=True)
            loss = loss_fn(model(history, context), targets)
            if not torch.isfinite(loss):
                raise ValueError("Training loss is non-finite")
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0, error_if_nonfinite=True)
            optimizer.step()
            total += loss.item() * len(targets)
        logs = _predict_logs(model, validation)
        refs = prepared["splits"]["validation"]
        actual = prepared["logs"][refs[:, 0], refs[:, 1], :2]
        errors = np.abs(logs - actual)
        validation_loss = float(np.where(errors < 1, errors ** 2 / 2, errors - 0.5).mean())
        curve.append({"epoch": epoch + 1, "train_huber": total / len(train), "validation_huber": validation_loss})
        if validation_loss < best:
            best, best_state, stale = validation_loss, copy.deepcopy(model.state_dict()), 0
        else:
            stale += 1
            if stale >= 5:
                break
    model.load_state_dict(best_state)
    name = "gru_flow_only" if flow_only else "gru"
    checkpoint = output / f"{name}_seed{seed}.pt"
    torch.save(model.state_dict(), checkpoint)
    loaded = load_model(checkpoint, flow_only=flow_only)
    check = Windows(prepared, prepared["splits"]["test"][:32], flow_only)
    original, replay = _predict_logs(model, check), _predict_logs(loaded, check)
    if not np.array_equal(original, replay):
        raise ValueError("Saved checkpoint does not reproduce its predictions")
    return model, {"model": name, "seed": seed, "epochs": len(curve), "best_validation_huber": best,
                   "checkpoint": checkpoint.name, "checkpoint_replay": "identical", "curve": curve}


def ridge_features(prepared: dict, refs: np.ndarray) -> np.ndarray:
    """All lag and rolling inputs end strictly before each target hour."""
    logs, cycles, identities = prepared["logs"], prepared["cycles"], prepared["identities"]
    result = []
    for series, hour in refs:
        pieces = [logs[series, hour - lag, :2] for lag in (1, 24, 168)]
        for width in (24, 168):
            window = logs[series, hour - width:hour, :2]
            pieces.extend((window.mean(axis=0), window.std(axis=0)))
        result.append(np.concatenate((*pieces, cycles[hour], identities[series])))
    return np.asarray(result, dtype=np.float64)


def metrics(actual: np.ndarray, predicted: np.ndarray) -> dict:
    delta = np.abs(actual - predicted)
    denominator = float(np.abs(actual).sum())
    return {"mae": float(delta.mean()), "wape": float(delta.sum() / denominator) if denominator else None,
            "log_mae": float(np.abs(np.log1p(actual) - np.log1p(predicted)).mean())}


def _metrics_for(prepared, refs, predicted, model, seed, split):
    output = []
    actual = prepared["values"][refs[:, 0], refs[:, 1], :2]
    for series, (token, category) in enumerate(prepared["keys"]):
        mask = refs[:, 0] == series
        if not mask.any():
            continue
        for direction, column in (("inflow", 0), ("outflow", 1)):
            output.append({"model": model, "seed": seed, "split": split, "token": token,
                           "category": category, "direction": direction, "samples": int(mask.sum()),
                           **metrics(actual[mask, column], predicted[mask, column])})
        output.append({"model": model, "seed": seed, "split": split, "token": token,
                       "category": category, "direction": "net", "samples": int(mask.sum()),
                       "mae": float(np.abs((actual[mask, 0] - actual[mask, 1]) -
                                            (predicted[mask, 0] - predicted[mask, 1])).mean()),
                       "wape": None, "log_mae": None})
    return output


def calibrate(prepared: dict, calibration_predictions: np.ndarray) -> dict:
    """No test value is accepted here: thresholds use the calibration split only."""
    refs = prepared["splits"]["calibration"]
    if (calibration_predictions.shape != (len(refs), 2) or
            not np.isfinite(calibration_predictions).all() or np.any(calibration_predictions < 0)):
        raise ValueError("Calibration predictions must match the calibration targets and be finite nonnegative flows")
    actual = prepared["values"][refs[:, 0], refs[:, 1], :2]
    residuals = np.abs(np.log1p(actual) - np.log1p(calibration_predictions))
    result = {}
    for series, key in enumerate(prepared["keys"]):
        mask = refs[:, 0] == series
        if not mask.any():
            continue
        by_direction = {}
        for direction, column in (("inflow", 0), ("outflow", 1)):
            distribution = np.sort(residuals[mask, column])
            by_direction[direction] = {"threshold": float(np.quantile(distribution, 0.95)),
                                       "samples": len(distribution), "sorted_residuals": distribution.tolist()}
        result[key] = by_direction
    return result


def _prediction_rows(prepared, refs, predicted, model, seed, split, calibration=None):
    result = []
    for (series, hour), values in zip(refs, predicted):
        token, category = prepared["keys"][series]
        actual = prepared["values"][series, hour, :2]
        residual = actual - values
        logs = np.abs(np.log1p(actual) - np.log1p(values))
        row = {"hour": iso(prepared["times"][hour]), "token": token, "category": category,
               "split": split, "model": model, "seed": seed, "actual_inflow": float(actual[0]),
               "actual_outflow": float(actual[1]), "predicted_inflow": float(values[0]),
               "predicted_outflow": float(values[1]), "residual_inflow": float(residual[0]),
               "residual_outflow": float(residual[1]), "log_residual_inflow": float(logs[0]),
               "log_residual_outflow": float(logs[1]), "anomaly": False, "score": None,
               "threshold_inflow": None, "threshold_outflow": None}
        if calibration is not None and split in ("calibration", "test") and (token, category) in calibration:
            flag, scores = False, []
            for column, direction in enumerate(("inflow", "outflow")):
                reference = calibration[token, category][direction]
                row[f"threshold_{direction}"] = reference["threshold"]
                flag |= logs[column] > reference["threshold"]
                scores.append(np.searchsorted(reference["sorted_residuals"], logs[column], side="right") / reference["samples"])
            row["score"] = float(max(scores))
            row["anomaly"] = bool(flag and split == "test")
        result.append(row)
    return result


def group_anomalies(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        if row["anomaly"] and row["model"] == "gru" and row["seed"] == 42 and row["split"] == "test":
            groups[row["token"], row["category"]].append(row)
    events = []
    for (token, category), flagged in groups.items():
        runs = []
        for row in sorted(flagged, key=lambda r: r["hour"]):
            if not runs or _datetime(row["hour"]) - _datetime(runs[-1][-1]["hour"]) != timedelta(hours=1):
                runs.append([])
            runs[-1].append(row)
        for run in runs:
            peak = max(run, key=lambda r: (r["score"], max(r["log_residual_inflow"], r["log_residual_outflow"])))
            directions = [direction for direction in ("inflow", "outflow")
                          if any(r[f"log_residual_{direction}"] > r[f"threshold_{direction}"] for r in run)]
            events.append({"token": token, "category": category, "start": run[0]["hour"],
                           "end": iso(_datetime(run[-1]["hour"]) + timedelta(hours=1)),
                           "hours": len(run), "score": peak["score"], "peak_hour": peak["hour"],
                           "directions": directions, "actual_inflow": sum(r["actual_inflow"] for r in run),
                           "actual_outflow": sum(r["actual_outflow"] for r in run),
                           "predicted_inflow": sum(r["predicted_inflow"] for r in run),
                           "predicted_outflow": sum(r["predicted_outflow"] for r in run),
                           "peak_log_residual": max(peak["log_residual_inflow"], peak["log_residual_outflow"]),
                           "investigation": "unexplained", "evidence": [],
                           "note": "Residual flag only. Transaction evidence is required before attributing a cause."})
    events.sort(key=lambda e: (-e["score"], -e["peak_log_residual"], e["start"], e["token"], e["category"]))
    for rank, event in enumerate(events, 1):
        event["rank"] = rank
        event["id"] = f"{event['token']}-{event['category']}-{event['start']}"
    return events


def run_experiment(rows, manifest, output: Path, *, epochs=50, seeds=(7, 42, 2026), lookback=168) -> dict:
    """Train frozen models; export comparable forecasts, metrics, and residual events."""
    if not isinstance(epochs, int) or not 1 <= epochs <= 50:
        raise ValueError("epochs must be an integer from 1 to 50")
    seeds = tuple(seeds)
    if 42 not in seeds or len(set(seeds)) != len(seeds) or any(not isinstance(s, int) or s < 0 or s >= 2 ** 32 for s in seeds):
        raise ValueError("Seeds must be distinct nonnegative 32-bit integers and include the prespecified main seed 42")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(min(2, os.cpu_count() or 1))
    torch.use_deterministic_algorithms(True)
    prepared = prepare(rows, manifest, lookback)
    metric_rows, prediction_rows, training = [], [], []
    ridge_x = {split: ridge_features(prepared, refs) for split, refs in prepared["splits"].items()}
    train_refs = prepared["splits"]["train"]
    ridge = make_pipeline(StandardScaler(), Ridge(alpha=1.0))
    ridge.fit(ridge_x["train"], prepared["logs"][train_refs[:, 0], train_refs[:, 1], :2])
    ridge_model, ridge_scaler = ridge[-1], ridge[0]
    write_json(output / "ridge.json", {"alpha": 1.0, "mean": ridge_scaler.mean_.tolist(),
                                     "scale": ridge_scaler.scale_.tolist(),
                                     "coef": ridge_model.coef_.tolist(), "intercept": ridge_model.intercept_.tolist(),
                                     "feature_order": [f"log_{direction}_lag{lag}" for lag in (1, 24, 168)
                                                       for direction in ("inflow", "outflow")] +
                                                      [f"log_{direction}_{stat}{width}" for width in (24, 168)
                                                       for stat in ("mean", "std") for direction in ("inflow", "outflow")] +
                                                      ["hour_sin", "hour_cos", "weekday_sin", "weekday_cos"] +
                                                      list(TOKENS) + list(CATEGORIES)})
    for split, refs in prepared["splits"].items():
        if split == "train":
            continue
        series, hours = refs[:, 0], refs[:, 1]
        predictions = {"lag24": prepared["values"][series, hours - 24, :2],
                       "lag168": prepared["values"][series, hours - 168, :2],
                       "weekly_median": np.median(np.stack([prepared["values"][series, hours - lag, :2]
                                                             for lag in (168, 336, 504, 672)]), axis=0),
                       "ridge": _original(ridge.predict(ridge_x[split]))}
        for name, predicted in predictions.items():
            metric_rows.extend(_metrics_for(prepared, refs, predicted, name, None, split))
            prediction_rows.extend(_prediction_rows(prepared, refs, predicted, name, None, split))
    baseline_scores = {}
    for name in BASELINES:
        scores = [m["wape"] for m in metric_rows if m["model"] == name and m["split"] == "validation" and m["wape"] is not None]
        baseline_scores[name] = float(np.mean(scores)) if scores else None
    available = [name for name in BASELINES if baseline_scores[name] is not None]
    best_baseline = min(available, key=lambda n: baseline_scores[n]) if available else min(
        BASELINES, key=lambda n: np.mean([m["mae"] for m in metric_rows if m["model"] == n and m["split"] == "validation"]))
    main_calibration = None
    specifications = [("gru", seed, False) for seed in seeds] + [("gru_flow_only", 42, True)]
    for name, seed, flow_only in specifications:
        model, record = _train(prepared, seed, epochs, flow_only, output)
        training.append(record)
        predicted = {split: _original(_predict_logs(model, Windows(prepared, refs, flow_only)))
                     for split, refs in prepared["splits"].items() if split != "train"}
        calibration = calibrate(prepared, predicted["calibration"]) if name == "gru" and seed == 42 else None
        if calibration is not None:
            main_calibration = calibration
        for split, values in predicted.items():
            refs = prepared["splits"][split]
            metric_rows.extend(_metrics_for(prepared, refs, values, name, seed, split))
            prediction_rows.extend(_prediction_rows(prepared, refs, values, name, seed, split, calibration))
    all_events = group_anomalies(prediction_rows)
    macros = []
    for name, seed in [(n, None) for n in BASELINES] + [(n, s) for n, s, _ in specifications]:
        for split in ("validation", "calibration", "test"):
            scores = [m["wape"] for m in metric_rows if m["model"] == name and m["seed"] == seed and m["split"] == split and m["wape"] is not None]
            macros.append({"model": name, "seed": seed, "split": split, "macro_wape": float(np.mean(scores)) if scores else None})
    result = {"status": "complete", "kind": manifest["kind"], "metrics": metric_rows, "macro_metrics": macros,
              "best_baseline": best_baseline, "baseline_validation_macro_wape": baseline_scores,
              "models": [record["model"] + "_seed" + str(record["seed"]) for record in training],
              "split_boundaries": prepared["boundaries"], "anomalies": all_events[:20],
              "anomaly_events_total": len(all_events), "features": list(NUMERIC_FEATURES),
              "predictions_path": str((output / "predictions.parquet").resolve()),
              "lookback": lookback, "warmup": WARMUP, "missing_targets": prepared["missing_targets"],
              "target_counts": {split: len(refs) for split, refs in prepared["splits"].items()},
              "seed_variability": {"seeds": list(seeds), "test_macro_wape": [m["macro_wape"] for m in macros
                                     if m["model"] == "gru" and m["split"] == "test"]},
              "claim": "Comparative historical backtest; residual flags have no independent anomaly truth labels."}
    versions = {"python": platform.python_version(), "numpy": np.__version__, "torch": torch.__version__, "sklearn": sklearn.__version__}
    write_json(output / "preprocessing.json", {**prepared["preprocessing"], "lookback": lookback,
                                               "warmup": WARMUP, "split_boundaries": prepared["boundaries"],
                                               "versions": versions, "hidden_size": 32, "layers": 1,
                                               "loss": "HuberLoss(log1p native token units)", "batch_size": 256,
                                               "learning_rate": 1e-3, "patience": 5, "gradient_clip": 1.0,
                                               "max_epochs": epochs, "seeds": list(seeds),
                                               "ablation_features": ["inflow", "outflow"],
                                               "feature_order": list(NUMERIC_FEATURES) +
                                                                ["hour_sin", "hour_cos", "weekday_sin", "weekday_cos"],
                                               "context_order": list(TOKENS) + list(CATEGORIES) +
                                                                ["target_hour_sin", "target_hour_cos", "target_weekday_sin", "target_weekday_cos"],
                                               "cpu_threads": torch.get_num_threads(), "deterministic": True,
                                               "checkpoint_replay_tolerance": 0.0})
    write_json(output / "training.json", training)
    write_json(output / "calibration.json", [{"token": token, "category": category, "directions": directions}
                                             for (token, category), directions in main_calibration.items()])
    write_json(output / "metrics.json", result)
    write_json(output / "anomalies.json", all_events)
    write_parquet(output / "predictions.parquet", prediction_rows)
    return result
