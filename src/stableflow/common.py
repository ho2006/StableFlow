"""Shared snapshot contracts; money remains integer micro-token units on disk."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

TOKENS = {
    "USDC": {"address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "decimals": 6},
    "USDT": {"address": "0xdac17f958d2ee523a2206206994597c13d831ec7", "decimals": 6},
}
CATEGORIES = ("cex", "dex", "lending", "bridge")
ZERO = "0x" + "0" * 40
NUMERIC_FEATURES = ("inflow", "outflow", "inflow_count", "outflow_count", "active_senders", "active_receivers")


def utc(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_json(value):
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        return clean_json(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite JSON value")
    return value


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(clean_json(value), ensure_ascii=False, indent=2, allow_nan=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_parquet(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise ValueError(f"Cannot write empty untyped table: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(clean_json(rows), allow_nan=False), encoding="utf-8")
    try:
        with duckdb.connect() as db:
            cursor = db.execute("SELECT * FROM read_json_auto(?) LIMIT 0", [str(temporary)])
            columns = [entry[0] for entry in cursor.description]
            quoted = {name: '"'+name.replace('"','""')+'"' for name in columns}
            projection = ', '.join(f'CAST({quoted[name]} AS VARCHAR) AS {quoted[name]}' if name.endswith('_raw') else quoted[name] for name in columns)
            db.execute(f"COPY (SELECT {projection} FROM read_json_auto($source)) TO $target (FORMAT PARQUET)", {"source": str(temporary), "target": str(path)})
    finally:
        temporary.unlink(missing_ok=True)


def read_parquet(path: Path) -> list[dict]:
    with duckdb.connect() as db:
        result = db.execute("SELECT * FROM read_parquet(?)", [str(path)])
        columns = [column[0] for column in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]


def load_snapshot(path: Path) -> tuple[dict, list[dict]]:
    manifest = read_json(path / "manifest.json")
    if manifest.get("schema_version") != 1 or manifest.get("kind") not in ("real", "synthetic-test"):
        raise ValueError("Unsupported snapshot schema or evidence kind")
    if not {"hourly.parquet", "edges.parquet", "totals.parquet", "labels.json"} <= set(manifest.get("files", {})):
        raise ValueError("Snapshot manifest lacks required file checksums")
    for name, digest in manifest["files"].items():
        target = (path / name).resolve()
        if not target.is_relative_to(path.resolve()):
            raise ValueError("Snapshot path escapes its directory")
        if sha256(target) != digest:
            raise ValueError(f"Snapshot checksum mismatch: {name}")
    return manifest, read_parquet(path / "hourly.parquet")


def finalize_snapshot(path: Path, metadata: dict) -> dict:
    files = {p.name: sha256(p) for p in sorted(path.iterdir()) if p.is_file() and p.name != "manifest.json" and not p.name.endswith(".tmp")}
    manifest = {"schema_version": 1, **metadata, "files": files}
    write_json(path / "manifest.json", manifest)
    return manifest
