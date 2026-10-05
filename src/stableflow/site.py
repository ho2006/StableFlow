"""Build an offline research site; no credentials, API calls, or browser ML."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from plotly.offline import get_plotlyjs

from .common import clean_json, read_json, read_parquet, write_json


WARNING = "Synthetic test data — not on-chain evidence"


def _safe_json(value) -> str:
    return json.dumps(clean_json(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ("'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value) for key, value in row.items()})


def build_site(snapshot: Path, analysis: dict, experiment: dict, output: Path) -> Path:
    """Generate index.html and local Plotly/assets from inspectable snapshot rows."""
    snapshot, output = Path(snapshot), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = analysis.get("manifest") or read_json(snapshot / "manifest.json")
    predictions = experiment.get("predictions", [])
    predictions_path = experiment.get("predictions_path")
    if predictions_path and Path(predictions_path).is_file():
        predictions = read_parquet(Path(predictions_path))
    baseline = experiment.get("best_baseline")
    display_models = {"gru", "gru_flow_only"}
    if isinstance(baseline, str):
        display_models.add(baseline)
    display_predictions = [row for row in predictions if row.get("split") == "test" and row.get("model") in display_models and row.get("seed") in (None, 42)]
    labels = read_json(snapshot / "labels.json") if (snapshot / "labels.json").is_file() else []
    evidence = read_json(snapshot / "evidence.json") if (snapshot / "evidence.json").is_file() else {}
    payload = clean_json({
        "manifest": manifest,
        "hourly": analysis.get("hourly", []),
        "edges": analysis.get("edges", []),
        "totals": analysis.get("totals", []),
        "quality": analysis.get("quality", {}),
        "findings": analysis.get("findings", []),
        "experiment": {key: value for key, value in experiment.items() if key not in ("predictions", "predictions_path")},
        "predictions": display_predictions,
        "labels": labels,
        "evidence": evidence,
    })
    write_json(output / "data.json", payload)
    downloads = output / "downloads"
    downloads.mkdir(exist_ok=True)
    for name, rows in (("hourly", payload["hourly"]), ("flows", payload["edges"]), ("predictions", predictions), ("metrics", experiment.get("metrics", []))):
        if manifest.get("kind") == "synthetic-test":
            rows = [{"evidence_notice": WARNING, **row} for row in rows]
        _csv(downloads / f"{name}.csv", rows)
    write_json(downloads / "evidence.json", {"manifest": manifest, "labels": labels, "evidence": evidence, "quality": payload["quality"]})
    (output / "plotly.min.js").write_text(get_plotlyjs(), encoding="utf-8")
    root = Path(__file__).parent
    document = (root / "site.html").read_text(encoding="utf-8")
    for marker, content in (
        ("__STYLES__", (root / "site.css").read_text(encoding="utf-8")),
        ("__SCRIPT__", (root / "site.js").read_text(encoding="utf-8")),
        ("__EVIDENCE_WARNING__", html.escape(WARNING) if manifest.get("kind") == "synthetic-test" else ""),
        ("__DATA__", _safe_json(payload)),
    ):
        document = document.replace(marker, content)
    result = output / "index.html"
    result.write_text(document, encoding="utf-8")
    return result
