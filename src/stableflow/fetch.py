"""Budgeted BigQuery acquisition and provenance-checked manual imports.

No import of Google credentials happens until fetch() is called. SQL export and
CSV import work offline. A failed query is never presented as complete data.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from .common import CATEGORIES, TOKENS, finalize_snapshot, iso, read_json, sha256, utc, write_json, write_parquet

GIB = 1024 ** 3
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
ROLES = {
    "timestamp": ("block_timestamp",),
    "token": ("token_address", "address", "contract_address"),
    "sender": ("from_address",),
    "receiver": ("to_address",),
    "amount": ("value", "quantity", "amount"),
    "transaction": ("transaction_hash",),
    "index": ("log_index",),
    "block": ("block_number",),
}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _month_after(value: datetime) -> datetime:
    return value.replace(year=value.year + (value.month == 12), month=value.month % 12 + 1)


def _months(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    if start.day != 1 or end.day != 1 or any((v.hour or v.minute or v.second or v.microsecond) for v in (start, end)):
        raise ValueError("Research bounds must be complete UTC calendar months")
    result = []
    while start < end:
        following = _month_after(start)
        result.append((start, following))
        start = following
    if not result:
        raise ValueError("Empty or reversed research interval")
    return result


def _config(path: Path | str) -> tuple[dict, list[dict]]:
    path = Path(path).resolve()
    config = read_json(path)
    for key in ("labels_file", "ledger_file", "schema_file", "evidence_directory"):
        if config.get(key):
            config[key] = str((path.parent / config[key]).resolve())
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_]+", config.get("dataset", "")):
        raise ValueError("Invalid BigQuery dataset identifier")
    _months(utc(config["start"]), utc(config["end"]))
    budget, reserve = float(config.get("budget_gib", 800)), float(config.get("reserve_gib", 200))
    if not 0 < reserve < budget <= 800:
        raise ValueError("Budget must be at most 800 GiB and include a positive verification reserve")
    config["budget_gib"],config["reserve_gib"] = budget,reserve
    count = config.get("evidence_per_month",20)
    if isinstance(count,bool) or int(count)!=float(count) or not 0<=int(count)<=100:
        raise ValueError("evidence_per_month must be an integer between zero and 100")
    config["evidence_per_month"] = int(count)
    labels = read_json(Path(config["labels_file"]))
    grouped = {}
    for label in labels:
        if not label.get("verified"):
            continue
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", label.get("address", "")) or label.get("category") not in CATEGORIES:
            raise ValueError("Invalid verified address label")
        if not label.get("source_url") or not label.get("evidence") or not label.get("retrieved_at"):
            raise ValueError("Verified labels require dated public evidence")
        label = {**label, "address": label["address"].lower()}
        grouped.setdefault(label["address"], []).append(label)
    # Conflict means unknown. Identical repeated evidence is harmless.
    confirmed = [v[0] for v in grouped.values() if len({(x["entity"], x["category"], x.get("valid_from"), x.get("valid_to")) for x in v}) == 1]
    config["categories"] = sorted({label["category"] for label in confirmed})
    config["labels_sha256"] = sha256(Path(config["labels_file"]))
    return config, confirmed


def _schema_fields(schema) -> list[dict]:
    return [{"name": field.name, "type": field.field_type, "mode": field.mode} for field in schema]


def _mapping(fields: list[dict], overrides: dict | None = None) -> dict:
    columns = {f.get("name", f.get("column_name")): f for f in fields}
    mapping = {}
    for role, candidates in ROLES.items():
        matches = [c for c in candidates if c in columns]
        chosen = (overrides or {}).get(role)
        if chosen is None:
            if len(matches) != 1:
                raise ValueError(f"Schema needs explicit schema_map.{role}; confirmed candidates: {matches}")
            chosen = matches[0]
        if chosen not in columns or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", chosen):
            raise ValueError(f"Unconfirmed column for {role}: {chosen}")
        field = columns[chosen]
        dtype = field.get("type", field.get("data_type", ""))
        if field.get("mode") == "REPEATED" or dtype.startswith("ARRAY") or dtype in ("RECORD", "STRUCT", "FLOAT", "FLOAT64"):
            raise ValueError(f"Unsupported or imprecise scalar field: {chosen} ({dtype})")
        allowed = {"timestamp":{"TIMESTAMP"},"token":{"STRING"},"sender":{"STRING"},"receiver":{"STRING"},"transaction":{"STRING"},"index":{"INT64","INTEGER"},"block":{"INT64","INTEGER"},"amount":{"STRING","INT64","INTEGER","NUMERIC","BIGNUMERIC"}}[role]
        if dtype not in allowed:
            raise ValueError(f"Unsupported source type for {role}: {dtype}")
        mapping[role] = chosen
    return mapping


def _sql_literal(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def aggregate_sql(config: dict, labels: list[dict], mapping: dict, start: datetime, end: datetime) -> str:
    """One scan plan returns typed JSON rows, with exact integer money strings."""
    dataset = config["dataset"]
    cols = {key: "`" + name + "`" for key, name in mapping.items()}
    label_values = []
    for label in labels:
        label_values.append("STRUCT(" + ", ".join(f"{_sql_literal(label.get(key))} AS {key}" for key in ("address", "entity", "category", "valid_from", "valid_to")) + ")")
    if not label_values:
        raise ValueError("At least one publicly verified label is needed")
    tokens = ",\n".join(f"STRUCT('{v['address']}' AS address, '{name}' AS token)" for name, v in TOKENS.items())
    # BIGNUMERIC is exact for supported values. ERROR fails overflow/hex/malformed
    # amounts instead of quietly erasing them with SAFE_CAST.
    return f"""-- StableFlow v1; UTC; native six-decimal token units. One month only.
-- Field names below were confirmed by get_table/INFORMATION_SCHEMA, not guessed.
WITH labels AS (SELECT * FROM UNNEST([{','.join(label_values)}])),
tokens AS (SELECT * FROM UNNEST([{tokens}])),
source_rows AS (
 SELECT t.{cols['timestamp']} AS ts, t.{cols['block']} AS block_number,
 t.{cols['transaction']} AS tx_hash, t.{cols['index']} AS log_index,
 LOWER(t.{cols['sender']}) AS sender, LOWER(t.{cols['receiver']}) AS receiver,
 tokens.token,
 IF(SAFE_CAST(t.{cols['amount']} AS BIGNUMERIC) IS NULL,
    ERROR('StableFlow: unsupported raw integer amount'), CAST(t.{cols['amount']} AS BIGNUMERIC)) AS amount_raw
 FROM `{dataset}.token_transfers` t JOIN tokens ON LOWER(t.{cols['token']}) = tokens.address
 WHERE t.{cols['timestamp']} >= TIMESTAMP('{iso(start)}') AND t.{cols['timestamp']} < TIMESTAMP('{iso(end)}')
), raw AS (
 SELECT tx_hash,log_index,ANY_VALUE(ts) AS ts,ANY_VALUE(block_number) AS block_number,
 ANY_VALUE(sender) AS sender,ANY_VALUE(receiver) AS receiver,ANY_VALUE(token) AS token,
 IF(COUNT(DISTINCT TO_JSON_STRING(STRUCT(ts,block_number,sender,receiver,token,amount_raw)))>1,
 ERROR('StableFlow: conflicting duplicated transaction/log event'),ANY_VALUE(amount_raw)) AS amount_raw
 FROM source_rows GROUP BY tx_hash,log_index
), checked AS (
 SELECT * REPLACE(IF(amount_raw < 0 OR amount_raw != TRUNC(amount_raw),
   ERROR('StableFlow: negative or fractional raw amount'), amount_raw) AS amount_raw) FROM raw
), classified AS (
 SELECT r.*, COALESCE(a.category,'unknown') AS from_category, COALESCE(b.category,'unknown') AS to_category,
 a.entity AS from_entity, b.entity AS to_entity
 FROM checked r
 LEFT JOIN labels a ON r.sender=a.address AND (a.valid_from IS NULL OR r.ts>=TIMESTAMP(a.valid_from)) AND (a.valid_to IS NULL OR r.ts<TIMESTAMP(a.valid_to))
 LEFT JOIN labels b ON r.receiver=b.address AND (b.valid_from IS NULL OR r.ts>=TIMESTAMP(b.valid_from)) AND (b.valid_to IS NULL OR r.ts<TIMESTAMP(b.valid_to))
), treated AS (
 SELECT *, CASE WHEN sender='{ZERO}' OR receiver='{ZERO}' THEN 'zero_address'
 WHEN sender=receiver THEN 'self' WHEN amount_raw=0 THEN 'zero_amount'
 WHEN from_entity=to_entity AND from_entity IS NOT NULL THEN 'internal'
 WHEN from_category=to_category THEN 'same_category' ELSE 'boundary' END AS treatment
 FROM classified
), boundary AS (
 SELECT TIMESTAMP_TRUNC(ts,HOUR) AS hour, token, to_category AS category, 'in' AS direction, amount_raw, sender,receiver
 FROM treated WHERE treatment='boundary'
 UNION ALL
 SELECT TIMESTAMP_TRUNC(ts,HOUR), token, from_category, 'out', amount_raw,sender,receiver
 FROM treated WHERE treatment='boundary'
), hourly AS (
 SELECT FORMAT_TIMESTAMP('%Y-%m-%dT%H:00:00Z',hour) AS hour, token, category,
 CAST(SUM(IF(direction='in',amount_raw,0)) AS STRING) AS inflow_raw,
 CAST(SUM(IF(direction='out',amount_raw,0)) AS STRING) AS outflow_raw,
 COUNTIF(direction='in') AS inflow_count, COUNTIF(direction='out') AS outflow_count,
 COUNT(DISTINCT sender) AS active_senders, COUNT(DISTINCT receiver) AS active_receivers
 FROM boundary GROUP BY hour,token,category
), edges AS (
 SELECT CAST(DATE(ts) AS STRING) AS date,token,from_category,to_category,treatment,
 CAST(SUM(amount_raw) AS STRING) AS amount_raw, COUNT(*) AS transfer_count
 FROM treated GROUP BY date,token,from_category,to_category,treatment
), totals AS (
 SELECT FORMAT_TIMESTAMP('%Y-%m-%dT%H:00:00Z',TIMESTAMP_TRUNC(ts,HOUR)) AS hour,token,
 CAST(SUM(amount_raw) AS STRING) AS amount_raw,COUNT(*) AS transfer_count,
 CAST(SUM(IF(from_category!='unknown' OR to_category!='unknown',amount_raw,0)) AS STRING) AS known_amount_raw
 FROM treated WHERE treatment NOT IN ('zero_address','self','zero_amount') GROUP BY hour,token
), coverage AS (
 SELECT FORMAT_TIMESTAMP('%Y-%m-%dT%H:00:00Z',TIMESTAMP_TRUNC(block_timestamp,HOUR)) AS hour,
 COUNT(DISTINCT block_number) AS block_count, MIN(block_number) AS min_block,MAX(block_number) AS max_block,
 TIMESTAMP_DIFF(MIN(block_timestamp),TIMESTAMP_TRUNC(MIN(block_timestamp),HOUR),SECOND)<=120
 AND TIMESTAMP_DIFF(TIMESTAMP_ADD(TIMESTAMP_TRUNC(MAX(block_timestamp),HOUR),INTERVAL 1 HOUR),MAX(block_timestamp),SECOND)<=120
 AND COUNT(DISTINCT block_number)=MAX(block_number)-MIN(block_number)+1 AS complete
 FROM `{dataset}.blocks` WHERE block_timestamp>=TIMESTAMP('{iso(start)}') AND block_timestamp<TIMESTAMP('{iso(end)}') GROUP BY hour
), evidence AS (
 SELECT FORMAT_TIMESTAMP('%Y-%m-%dT%H:00:00Z',TIMESTAMP_TRUNC(ts,HOUR)) AS hour,token,tx_hash,log_index,block_number,sender,receiver,
 from_category,to_category,treatment,CAST(amount_raw AS STRING) AS amount_raw
 FROM treated WHERE treatment='boundary' ORDER BY amount_raw DESC,tx_hash,log_index LIMIT {int(config.get('evidence_per_month',20))}
)
SELECT 'hourly' AS row_type,TO_JSON_STRING(h) AS payload FROM hourly h
UNION ALL SELECT 'edges',TO_JSON_STRING(e) FROM edges e
UNION ALL SELECT 'totals',TO_JSON_STRING(t) FROM totals t
UNION ALL SELECT 'coverage',TO_JSON_STRING(c) FROM coverage c
UNION ALL SELECT 'evidence',TO_JSON_STRING(e) FROM evidence e
"""


def export_sql(config_path: Path | str, output: Path | str) -> dict:
    config, labels = _config(config_path)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    metadata = f"SELECT table_name,column_name,data_type FROM `{config['dataset']}.INFORMATION_SCHEMA.COLUMNS` WHERE table_name IN ('token_transfers','blocks','logs','receipts') ORDER BY table_name,ordinal_position;\n"
    (output / "metadata.sql").write_text(metadata, encoding="utf-8")
    if not config.get("schema_file"):
        return {"status": "schema-required", "metadata_sql": str(output / "metadata.sql"), "message": "Export metadata query as JSON, then set schema_file; amount columns are never guessed."}
    fields = read_json(Path(config["schema_file"]))
    if isinstance(fields, dict):
        fields = fields["token_transfers"]
    else:
        fields = [f for f in fields if f.get("table_name", "token_transfers") == "token_transfers"]
    mapping = _mapping(fields, config.get("schema_map"))
    queries = {}
    for start, end in _months(utc(config["start"]), utc(config["end"])):
        sql = aggregate_sql(config, labels, mapping, start, end)
        name = start.strftime("%Y-%m") + ".sql"
        (output / name).write_text(sql, encoding="utf-8")
        queries[name] = _digest(sql)
    write_json(output / "export.json", {"source": config["dataset"], "start": config["start"], "end": config["end"], "schema_map": mapping, "labels_sha256": config["labels_sha256"], "queries": queries})
    return {"status": "ready", "directory": str(output), "queries": queries}


def _job_config(dry: bool, maximum: int | None = None):
    from google.cloud.bigquery import QueryJobConfig
    return QueryJobConfig(dry_run=dry, use_query_cache=False, maximum_bytes_billed=maximum)


def _column(fields: list[dict], candidates: tuple[str, ...]) -> str:
    available = {field["name"] for field in fields}
    found = [name for name in candidates if name in available]
    if len(found) != 1:
        raise ValueError(f"Unsupported verification schema; expected one of {candidates}, found {found}")
    return "`"+found[0]+"`"


def evidence_sql(config: dict, labels: list[dict], schema: dict, mapping: dict, window: dict) -> str:
    """Bounded top-five transfers per anomaly window, joined to raw logs/receipts."""
    start,end = utc(window["start"]),utc(window["end"])
    if not start < end <= start + timedelta(days=31):
        raise ValueError("Evidence window must be positive and at most 31 days")
    if window["token"] not in TOKENS or window["category"] not in (*config["categories"],"unknown"):
        raise ValueError("Evidence window uses an uncovered token/category")
    logs,receipts = schema["logs"],schema["receipts"]
    lt,li,la,ld,lp = (_column(logs,c) for c in (("transaction_hash",),("log_index",),("address",),("data",),("topics",)))
    rt,rs = (_column(receipts,c) for c in (("transaction_hash",),("status","receipt_status")))
    base = aggregate_sql(config,labels,mapping,start,end).split(", boundary AS (")[0]
    dataset = config["dataset"]
    return base+f""", selected AS (
 SELECT * FROM treated WHERE treatment='boundary' AND token='{window['token']}'
 AND (from_category='{window['category']}' OR to_category='{window['category']}')
 ORDER BY amount_raw DESC,tx_hash,log_index LIMIT 5
), verified AS (
 SELECT FORMAT_TIMESTAMP('%Y-%m-%dT%H:00:00Z',TIMESTAMP_TRUNC(e.ts,HOUR)) AS hour,
 e.token,e.tx_hash,e.log_index,e.block_number,e.sender,e.receiver,e.from_category,e.to_category,e.treatment,
 CAST(e.amount_raw AS STRING) AS amount_raw,LOWER(l.{la}) AS log_address,l.{ld} AS log_data,l.{lp} AS log_topics,
 CAST(r.{rs} AS STRING) AS receipt_status
 FROM selected e LEFT JOIN `{dataset}.logs` l ON e.tx_hash=l.{lt} AND e.log_index=l.{li}
 AND l.block_timestamp>=TIMESTAMP('{iso(start)}') AND l.block_timestamp<TIMESTAMP('{iso(end)}')
 LEFT JOIN `{dataset}.receipts` r ON e.tx_hash=r.{rt}
 AND r.block_timestamp>=TIMESTAMP('{iso(start)}') AND r.block_timestamp<TIMESTAMP('{iso(end)}')
)
SELECT 'evidence' AS row_type,TO_JSON_STRING(v) AS payload FROM verified v
"""


def _verify_evidence(rows: list[dict], window: dict | None = None) -> list[dict]:
    events = _decode(rows)["evidence"]
    for event in events:
        topics = event.get("log_topics") or []
        reason = []
        if event.get("log_address") != TOKENS[event["token"]]["address"]:
            reason.append("missing or mismatched token log address")
        if len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
            reason.append("missing or invalid ERC-20 Transfer topics")
        elif "0x"+str(topics[1])[-40:].lower() != event["sender"] or "0x"+str(topics[2])[-40:].lower() != event["receiver"]:
            reason.append("log endpoint mismatch")
        try:
            if not re.fullmatch(r"0x[0-9a-fA-F]{64}",str(event.get("log_data"))) or int(event["log_data"],16) != int(event["amount_raw"]):
                reason.append("log amount mismatch")
        except (TypeError,ValueError):
            reason.append("missing or malformed log amount")
        if str(event.get("receipt_status")).lower() not in ("1","true","0x1","success"):
            reason.append("missing or unsuccessful receipt")
        event["log_receipt_verified"] = not reason
        event["verification_errors"] = reason
        if window:
            event["event_id"] = window.get("id")
    return events


def fetch_evidence(config_path: Path | str, snapshot: Path | str, windows: list[dict], output: Path | str, *, client=None) -> dict:
    """Use the 200-GiB reserve to inspect at most 20 already-selected events.

    Output is an attachment; the frozen snapshot itself remains unchanged.
    No business-event conclusion is inferred from a large transfer alone.
    """
    from .common import load_snapshot
    config,labels = _config(config_path)
    snapshot,output = Path(snapshot),Path(output)
    manifest,_ = load_snapshot(snapshot)
    if manifest["kind"] != "real" or manifest.get("labels_sha256") != config["labels_sha256"]:
        raise ValueError("Evidence queries require the same labels as a real frozen snapshot")
    if not 0 < len(windows) <= 20:
        raise ValueError("Select between one and twenty anomaly events")
    for window in windows:
        if utc(window["start"])<utc(manifest["start"]) or utc(window["end"])>utc(manifest["end"]):
            raise ValueError("Evidence window falls outside the frozen research interval")
    if client is None:
        from google.cloud import bigquery
        client = bigquery.Client(project=config["project_id"],location=config.get("location","US"))
    schema = {name:_schema_fields(client.get_table(config["dataset"]+"."+name).schema) for name in ("token_transfers","logs","receipts")}
    mapping = _mapping(schema["token_transfers"],config.get("schema_map"))
    output.mkdir(parents=True,exist_ok=True)
    results,jobs = [],[]
    with Budget(config) as budget:
        queries = [evidence_sql(config,labels,schema,mapping,window) for window in windows]
        estimates = [max(budget.estimate(client,sql),30*1024**2) for sql in queries]
        if sum(estimates) > budget.remaining("verification"):
            raise RuntimeError("Evidence queries exceed the verification budget; none were submitted")
        for index,(window,sql,estimate) in enumerate(zip(windows,queries,estimates)):
            (output / f"event-{index:02d}.sql").write_text(sql,encoding="utf-8")
            events = _verify_evidence(budget.execute(client,sql,estimate,"verification"),window)
            results.append({"id":window.get("id"),"window":window,"transfers":events,"assessment":"temporarily unexplained","assessment_basis":"Raw log and receipt verification only; independently corroborate business explanations."})
            jobs.append(dict(budget.ledger["queries"][-1]))
            write_json(output / "investigations.json",{"snapshot_manifest_sha256":sha256(snapshot / "manifest.json"),"events":results,"jobs":jobs})
    return {"status":"complete","events":results,"jobs":jobs}


class Budget:
    """Persistent single-process budget lock; uncertain submissions reserve the cap."""
    def __init__(self, config: dict):
        self.path = Path(config["ledger_file"])
        self.lock = self.path.with_suffix(self.path.suffix + ".lock")
        self.limit = int(config.get("budget_gib", 800) * GIB)
        self.data_limit = self.limit - int(config.get("reserve_gib", 200) * GIB)
        self.month = datetime.now(timezone.utc).strftime("%Y-%m")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError("Budget lock exists: another fetch may be running. Verify it has stopped before removing the lock.") from exc
        self.ledger = read_json(self.path) if self.path.exists() else {"version": 1, "queries": []}
        return self

    def __exit__(self, *_):
        os.close(self.fd)
        self.lock.unlink(missing_ok=True)

    def remaining(self, phase="data") -> int:
        self.month = datetime.now(timezone.utc).strftime("%Y-%m")
        spent = sum(int(q["bytes"]) for q in self.ledger["queries"] if q["billing_month"] == self.month)
        data_spent = sum(int(q["bytes"]) for q in self.ledger["queries"] if q["billing_month"] == self.month and q["phase"] == "data")
        return max(0, min(self.limit - spent, self.data_limit - data_spent) if phase == "data" else self.limit - spent)

    def estimate(self, client, sql: str) -> int:
        job = client.query(sql, job_config=_job_config(True))
        # These queries read two tables; BigQuery bills a minimum per table.
        return max(int(job.total_bytes_processed or 0), 20 * 1024 ** 2)

    def execute(self, client, sql: str, estimate: int, phase="data") -> list[dict]:
        # Keep the maximum equal to the dry-run reservation. Changed table sizes
        # fail closed rather than consuming the verification reserve.
        if estimate > self.remaining(phase):
            raise RuntimeError("Query exceeds remaining calendar billing-month budget; it was not submitted")
        entry = {"billing_month": self.month, "phase": phase, "sql_sha256": _digest(sql), "bytes": estimate, "status": "reserved", "job_id": "stableflow_"+uuid.uuid4().hex}
        self.ledger["queries"].append(entry)
        write_json(self.path, self.ledger)
        job = None
        try:
            # A fixed job id makes transport retries idempotent. Disable job
            # retries: a replacement job would spend outside this reservation.
            job = client.query(sql, job_config=_job_config(False, estimate),job_id=entry["job_id"],job_retry=None)
            entry["job_id"] = job.job_id
            write_json(self.path, self.ledger)
            result = [dict(row) for row in job.result(job_retry=None)]
            entry["bytes"] = int(job.total_bytes_billed if job.total_bytes_billed is not None else estimate)
            entry["status"] = "complete"
            return result
        except Exception:
            entry["status"] = "failed-or-uncertain"
            if job is not None and getattr(job, "total_bytes_billed", None) is not None:
                entry["bytes"] = int(job.total_bytes_billed)
            raise
        finally:
            write_json(self.path, self.ledger)


def _validate_raw(value) -> str:
    text = str(value)
    if not re.fullmatch(r"[0-9]+(?:\.0+)?", text):
        raise ValueError(f"Expected an exact nonnegative integer amount, got {text}")
    return str(int(text.split(".")[0]))


def _decode(rows: list[dict]) -> dict[str, list[dict]]:
    result = {key: [] for key in ("hourly", "edges", "totals", "coverage", "evidence")}
    seen = {}
    for outer in rows:
        name = outer["row_type"]
        if name not in result:
            raise ValueError(f"Unexpected query row type: {name}")
        row = json.loads(outer["payload"]) if isinstance(outer["payload"], str) else dict(outer["payload"])
        if row.get("token", "USDC") not in TOKENS:
            raise ValueError("Unexpected token in source export")
        for field in ("sender","receiver","log_address"):
            if row.get(field) is not None:
                if not re.fullmatch(r"0x[0-9a-fA-F]{40}",row[field]):
                    raise ValueError(f"Malformed Ethereum address: {field}")
                row[field] = row[field].lower()
        if "tx_hash" in row and not re.fullmatch(r"0x[0-9a-fA-F]{64}",row["tx_hash"]):
            raise ValueError("Malformed Ethereum transaction hash")
        if "hour" in row:
            row["hour"] = iso(utc(row["hour"]))
            if utc(row["hour"]).minute or utc(row["hour"]).second:
                raise ValueError("Source hour is not aligned to an hour boundary")
        for field in ("category", "from_category", "to_category"):
            if field in row and row[field] not in (*CATEGORIES, "unknown"):
                raise ValueError("Unrecognized endpoint category")
        if "treatment" in row and row["treatment"] not in ("boundary", "same_category", "internal", "self", "zero_address", "zero_amount"):
            raise ValueError("Unrecognized transfer treatment")
        for field in ("inflow_count", "outflow_count", "active_senders", "active_receivers", "transfer_count", "log_index", "block_number", "block_count", "min_block", "max_block"):
            if field in row:
                if isinstance(row[field], bool) or str(row[field]) != str(int(row[field])) or int(row[field]) < 0:
                    raise ValueError(f"Invalid nonnegative integer: {field}")
                row[field] = int(row[field])
        if name == "coverage":
            if not isinstance(row["complete"],bool):
                raise ValueError("Coverage complete must be a JSON boolean")
            block_count = int(row["block_count"])
            if block_count < 1 or int(row["min_block"]) < 0 or int(row["max_block"]) < int(row["min_block"]):
                raise ValueError("Invalid block coverage counts")
            if row["complete"] and block_count != int(row["max_block"])-int(row["min_block"])+1:
                raise ValueError("Discontinuous blocks cannot mark an hour complete")
        for field, value in list(row.items()):
            if field.endswith("_raw"):
                row[field] = _validate_raw(value)
                row[field[:-4]] = float(int(row[field]) / 10 ** 6)
                if not math.isfinite(row[field[:-4]]):
                    raise ValueError("Non-finite native token amount")
        if name=="totals" and int(row["known_amount_raw"])>int(row["amount_raw"]):
            raise ValueError("Known transfer amount cannot exceed total transfer amount")
        keys = {
            "hourly": ("hour", "token", "category"),
            "edges": ("date", "token", "from_category", "to_category", "treatment"),
            "totals": ("hour", "token"), "coverage": ("hour",),
            "evidence": ("tx_hash", "log_index"),
        }[name]
        key = (name,) + tuple(row[k] for k in keys)
        if key in seen and seen[key] != row:
            raise ValueError("Conflicting duplicate aggregate row; exports may use different labels or SQL")
        if key not in seen:
            seen[key] = row
            result[name].append(row)
    return result


def _snapshot(config: dict, labels: list[dict], rows: list[dict], output: Path, source: dict, complete_months: list[str]) -> dict:
    decoded = _decode(rows)
    start, end = utc(config["start"]), utc(config["end"])
    observations = {r["hour"]:r for r in decoded["coverage"]}
    ordered = sorted(observations)
    # Monthly SQL can prove continuity inside an hour. This checks gaps that
    # straddle hour and month boundaries before any zero filling is permitted.
    for previous,current in zip(ordered,ordered[1:]):
        if utc(current)-utc(previous)==timedelta(hours=1) and int(observations[current]["min_block"]) != int(observations[previous]["max_block"])+1:
            observations[previous]["complete"] = observations[current]["complete"] = False
            observations[previous]["boundary_gap"] = observations[current]["boundary_gap"] = True
    coverage = {h:bool(r["complete"]) for h,r in observations.items()}
    hours = [iso(start + timedelta(hours=i)) for i in range(int((end-start).total_seconds()/3600))]
    expected = set(hours)
    if any(row["hour"] not in expected for name in ("hourly", "totals", "coverage", "evidence") for row in decoded[name]):
        raise ValueError("Source rows fall outside research interval")
    for row in decoded["edges"]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",row["date"]) or not start<=utc(row["date"])<end:
            raise ValueError("Matrix source date falls outside research interval")
    covered_months = set(complete_months)
    categories = config["categories"] + ["unknown"]
    if any(r["category"] not in categories for r in decoded["hourly"]):
        raise ValueError("Hourly export contains an uncovered category")
    if any(r[key] not in categories for r in decoded["edges"] for key in ("from_category","to_category")):
        raise ValueError("Matrix export contains an uncovered category")
    hourly = {(r["hour"],r["token"],r["category"]): r for r in decoded["hourly"]}
    totals = {(r["hour"],r["token"]): r for r in decoded["totals"]}
    hour_rows, total_rows = [], []
    for hour in hours:
        complete = coverage.get(hour, False) and hour[:7] in covered_months
        for token in TOKENS:
            for category in categories:
                found = hourly.get((hour,token,category))
                row = {"hour": hour,"token":token,"category":category,"inflow_raw":"0","outflow_raw":"0","inflow":0.0,"outflow":0.0,"inflow_count":0,"outflow_count":0,"active_senders":0,"active_receivers":0, **(found or {}), "complete": complete}
                if not complete:
                    for field in row:
                        if field not in ("hour","token","category","complete"):
                            row[field] = None
                hour_rows.append(row)
            row = {"hour":hour,"token":token,"amount_raw":"0","amount":0.0,"transfer_count":0,"known_amount_raw":"0","known_amount":0.0,**totals.get((hour,token),{}),"complete":complete}
            if not complete:
                for field in ("amount_raw","amount","transfer_count","known_amount_raw","known_amount"):
                    row[field] = None
            total_rows.append(row)
    output.mkdir(parents=True,exist_ok=True)
    if (output / "manifest.json").exists():
        raise ValueError("Snapshot already finalized; choose a new output directory")
    write_parquet(output / "hourly.parquet", hour_rows)
    write_parquet(output / "totals.parquet", total_rows)
    if decoded["edges"]:
        write_parquet(output / "edges.parquet", decoded["edges"])
    else:
        with duckdb.connect() as db:
            db.execute("COPY (SELECT NULL::VARCHAR AS date,NULL::VARCHAR AS token,NULL::VARCHAR AS from_category,NULL::VARCHAR AS to_category,NULL::VARCHAR AS treatment,NULL::VARCHAR AS amount_raw,NULL::DOUBLE AS amount,NULL::BIGINT AS transfer_count WHERE false) TO ? (FORMAT PARQUET)",[str(output / "edges.parquet")])
    write_json(output / "labels.json", labels)
    write_json(output / "evidence.json", decoded["evidence"])
    write_json(output / "coverage.json", decoded["coverage"])
    if config.get("evidence_directory") and Path(config["evidence_directory"]).is_dir():
        shutil.make_archive(str(output / "source-evidence"), "zip", config["evidence_directory"])
    return finalize_snapshot(output, {"kind":"real","start":iso(start),"end":iso(end),"categories":config["categories"],"source":source,"complete_months":complete_months,"labels_sha256":config["labels_sha256"],"label_policy":"Fixed-label historical replay; null validity never implies contemporary identification.","coverage_policy":"Successful monthly token scan plus observed contiguous block numbers and hour-boundary coverage; indexer completeness cannot be proved by block table alone.","missing_hours":sum(not coverage.get(h,False) or h[:7] not in covered_months for h in hours)})


def fetch(config_path: Path | str, output: Path | str, *, client=None) -> dict:
    config, labels = _config(config_path)
    output = Path(output)
    if client is None:
        if not config.get("project_id"):
            raise ValueError("Set project_id to your unbilled BigQuery Sandbox project; then authenticate with ADC or use export-sql/import-csv")
        try:
            from google.cloud import bigquery
            client = bigquery.Client(project=config["project_id"],location=config.get("location","US"))
        except ImportError as exc:
            raise RuntimeError("Install the fetch extra: pip install -e '.[fetch]'") from exc
    schema = {}
    for name in ("token_transfers","blocks","logs","receipts"):
        schema[name] = _schema_fields(client.get_table(config["dataset"]+"."+name).schema)
    if not {"block_timestamp","block_number"}.issubset({f["name"] for f in schema["blocks"]}):
        raise ValueError("Unsupported blocks schema")
    mapping = _mapping(schema["token_transfers"],config.get("schema_map"))
    original_start = config["start"]
    queries, estimates = [], []
    with Budget(config) as budget:
        while True:
            months = _months(utc(config["start"]),utc(config["end"]))
            queries = [aggregate_sql(config,labels,mapping,s,e) for s,e in months]
            estimates = [budget.estimate(client,query) for query in queries]
            if sum(estimates) <= budget.remaining():
                break
            if len(months) > 6 and config.get("allow_six_month_fallback",True):
                config["start"] = iso(months[-6][0])
                continue
            raise RuntimeError("Six-month acquisition exceeds remaining budget; no live query was submitted")
        output.mkdir(parents=True,exist_ok=True)
        if (output / "manifest.json").exists():
            raise ValueError("Snapshot already finalized; choose a new output directory")
        write_json(output / "schema.json",schema)
        all_rows, completed, jobs = [], [], []
        # ponytail: local monthly JSON cache; cloud destinations only if result size becomes a bottleneck.
        for (start,end),query,estimate in zip(months,queries,estimates):
            month = start.strftime("%Y-%m")
            (output / (month+".sql")).write_text(query,encoding="utf-8")
            try:
                rows = budget.execute(client,query,estimate)
                _decode(rows)  # Reject malformed results before marking success.
                write_json(output / (month+".query.json"), rows)
                all_rows.extend(rows)
                completed.append(month)
                jobs.append(dict(budget.ledger["queries"][-1]))
                write_json(output / "fetch-state.json",{"complete_months":completed,"jobs":jobs,"start":config["start"],"end":config["end"]})
            except Exception as exc:
                write_json(output / "fetch-state.json",{"complete_months":completed,"jobs":jobs,"failed_month":month,"status":"incomplete","error":str(exc)})
                # Finalize the full chosen interval with explicit null failed hours.
                if all_rows:
                    _snapshot(config,labels,all_rows,output,{"type":"bigquery","dataset":config["dataset"],"jobs":jobs,"status":"incomplete"},completed)
                raise
        source = {"type":"bigquery","dataset":config["dataset"],"jobs":jobs,"schema_map":mapping,"requested_start":original_start,"scan_budget_gib":config.get("budget_gib",800),"reserve_gib":config.get("reserve_gib",200),"billing_month":budget.month,"status":"complete"}
        sample = _decode(all_rows)["evidence"]
        source["sample_verification"] = "no-boundary-sample"
        if sample:
            # Verify bounded samples at both ends, avoiding an expensive logs
            # scan for every month. These are QA samples, not anomaly reviews.
            sampled = {event["hour"]:event for event in (min(sample,key=lambda e:e["hour"]),max(sample,key=lambda e:e["hour"]))}
            try:
                windows = [{"start":event["hour"],"end":iso(utc(event["hour"])+timedelta(hours=1)),"token":event["token"],"category":event["from_category"] if event["from_category"]!="unknown" else event["to_category"]} for event in sampled.values()]
                verify_queries = [evidence_sql(config,labels,schema,mapping,window) for window in windows]
                verify_estimates = [max(budget.estimate(client,sql),30*1024**2) for sql in verify_queries]
                if sum(verify_estimates)>budget.remaining("verification"):
                    source["sample_verification"] = "deferred-budget"
                else:
                    verified = []
                    for index,(query,estimate) in enumerate(zip(verify_queries,verify_estimates)):
                        (output / f"verify-{index:02d}.sql").write_text(query,encoding="utf-8")
                        outer = budget.execute(client,query,estimate,"verification")
                        write_json(output / f"verify-{index:02d}.query.json",outer)
                        verified.extend(_verify_evidence(outer))
                        source["jobs"].append(dict(budget.ledger["queries"][-1]))
                    keyed = {(e["tx_hash"],e["log_index"]):e for e in verified}
                    for outer in all_rows:
                        if outer["row_type"]=="evidence":
                            event = json.loads(outer["payload"]) if isinstance(outer["payload"],str) else dict(outer["payload"])
                            if (event["tx_hash"],event["log_index"]) in keyed:
                                outer["payload"] = json.dumps(keyed[(event["tx_hash"],event["log_index"])])
                    source["sample_verification"] = "passed" if verified and all(e["log_receipt_verified"] for e in verified) else "needs-review"
                    source["verified_transfer_samples"] = len(verified)
            except Exception as exc:
                source["sample_verification"] = "failed-or-schema-unsupported"
                source["sample_verification_error"] = str(exc)
        return _snapshot(config,labels,all_rows,output,source,completed)


def import_csv(config_path: Path | str, input_dir: Path | str, output: Path | str) -> dict:
    """Import monthly union CSV exports only with matching frozen provenance."""
    config, labels = _config(config_path)
    input_dir,output = Path(input_dir),Path(output)
    if (output / "manifest.json").exists():
        raise ValueError("Snapshot already finalized; choose a new output directory")
    provenance = read_json(input_dir / "provenance.json")
    if provenance.get("dataset") != config["dataset"] or provenance.get("labels_sha256") != config["labels_sha256"]:
        raise ValueError("CSV provenance dataset/label digest does not match this config")
    config["start"],config["end"] = provenance["start"],provenance["end"]
    months = _months(utc(config["start"]),utc(config["end"]))
    schema = provenance["schema"]
    fields = schema["token_transfers"] if isinstance(schema,dict) else [f for f in schema if f.get("table_name","token_transfers")=="token_transfers"]
    mapping = _mapping(fields, config.get("schema_map"))
    rows, completed = [], []
    for start,end in months:
        month = start.strftime("%Y-%m")
        entry = provenance.get("months",{}).get(month)
        if not entry or entry.get("status") != "complete":
            continue
        path = input_dir / (month+".csv")
        expected = aggregate_sql(config,labels,mapping,start,end)
        if entry.get("sha256") != sha256(path) or entry.get("sql_sha256") != _digest(expected) or not entry.get("job_id"):
            raise ValueError(f"CSV checksum, SQL version or job provenance mismatch: {month}")
        with path.open(encoding="utf-8-sig",newline="") as stream:
            reader = csv.DictReader(stream)
            if set(reader.fieldnames or ()) != {"row_type","payload"}:
                raise ValueError("Expected monthly BigQuery union CSV columns row_type,payload")
            chunk = list(reader)
        _decode(chunk)
        rows.extend(chunk)
        completed.append(month)
    if not rows:
        raise ValueError("No complete source export exists; synthetic evidence is not substituted")
    output.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(input_dir / "provenance.json", output / "provenance.json")
    for start,end in months:
        month = start.strftime("%Y-%m")
        if month in completed:
            shutil.copyfile(input_dir / (month+".csv"), output / (month+".csv"))
            (output / (month+".sql")).write_text(aggregate_sql(config,labels,mapping,start,end),encoding="utf-8")
    source = {"type":"bigquery-manual-csv","dataset":config["dataset"],"provenance_sha256":sha256(input_dir / "provenance.json"),"status":"complete" if len(completed)==len(months) else "incomplete"}
    return _snapshot(config,labels,rows,output,source,completed)
