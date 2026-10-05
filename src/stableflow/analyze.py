"""Exact event accounting and source-backed descriptive results."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import duckdb

from .common import CATEGORIES, TOKENS, ZERO, finalize_snapshot, iso, load_snapshot, read_json, read_parquet, utc, write_json, write_parquet


def classify(address: str, when, labels: list[dict]) -> tuple[str, str | None]:
    matches = {(row["category"], row["entity"]) for row in labels
               if row.get("verified") is True and row["address"].lower() == address
               and (not row.get("valid_from") or utc(row["valid_from"]) <= when)
               and (not row.get("valid_to") or when < utc(row["valid_to"]))}
    return next(iter(matches)) if len(matches) == 1 else ("unknown", None)


def normalize_events(events: list[dict], labels: list[dict], start: str, end: str) -> tuple[list[dict], int]:
    begin, finish = utc(start), utc(end)
    tokens = {info["address"]: token for token, info in TOKENS.items()}
    unique = {}
    duplicates = 0
    for row in events:
        when = utc(row["block_timestamp"])
        if not begin <= when < finish:
            raise ValueError("Event outside declared snapshot interval")
        chain_text=str(row.get("chain_id", 1))
        if not chain_text.isascii() or not chain_text.isdecimal() or int(chain_text) != 1:
            raise ValueError("Only Ethereum mainnet is supported")
        token = tokens.get(row["token_address"].lower())
        if token is None:
            raise ValueError("Token contract is not in the issuer-verified allowlist")
        raw_text = str(row["amount_raw"])
        if not raw_text.isascii() or not raw_text.isdecimal() or len(raw_text) > 38:
            raise ValueError("Amount must be a nonnegative decimal integer of at most 38 digits")
        raw = int(raw_text)
        addresses = [row[key].lower() for key in ("from_address", "to_address")]
        for address in addresses:
            if len(address) != 42 or not address.startswith("0x"):
                raise ValueError("Invalid Ethereum address")
            int(address[2:], 16)
        tx = row["tx_hash"].lower()
        if len(tx) != 66 or not tx.startswith("0x"):
            raise ValueError("Invalid transaction hash")
        int(tx[2:], 16)
        index_text=str(row['log_index'])
        if not index_text.isascii() or not index_text.isdecimal():
            raise ValueError("Invalid log index")
        index=int(index_text)
        source, target = addresses
        from_category, from_entity = classify(source, when, labels)
        to_category, to_entity = classify(target, when, labels)
        if ZERO in addresses:
            treatment = "zero_address"
        elif source == target:
            treatment = "self"
        elif raw == 0:
            treatment = "zero_amount"
        elif from_entity is not None and from_entity == to_entity:
            treatment = "internal"
        elif from_category == to_category:
            treatment = "same_category"
        else:
            treatment = "boundary"
        normalized = {"block_timestamp":iso(when),"hour": iso(when.replace(minute=0, second=0, microsecond=0)), "date": when.date().isoformat(),
                      "token": token, "tx_hash": tx, "log_index": index, "from_address": source, "to_address": target,
                      "from_category": from_category, "to_category": to_category, "treatment": treatment, "amount_raw": str(raw)}
        key = (1, tx, index)
        if key in unique:
            if unique[key] != normalized:
                raise ValueError("Conflicting duplicate transfer event")
            duplicates += 1
        else:
            unique[key] = normalized
    return list(unique.values()), duplicates


def snapshot_from_events(events: list[dict], labels: list[dict], start: str, end: str, output: Path, *, kind: str,
                         complete_months: list[str], source: dict) -> dict:
    if kind not in ("real", "synthetic-test"):
        raise ValueError("Explicit evidence kind required")
    normalized, duplicates = normalize_events(events, labels, start, end)
    if not normalized:
        raise ValueError("No transfer events")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "labels.json", labels)
    write_json(output / "evidence.json", normalized[:30])
    write_parquet(output / "events.parquet", normalized)
    categories = sorted({row["category"] for row in labels if row.get("verified") is True and row["category"] in CATEGORIES})
    begin, finish = utc(start), utc(end)
    grid = [{"hour": iso(begin + timedelta(hours=i)), "month": (begin + timedelta(hours=i)).strftime("%Y-%m"),
             "token": token, "category": category} for i in range(int((finish - begin).total_seconds() // 3600))
            for token in TOKENS for category in (*categories, "unknown")]
    write_parquet(output / "grid.parquet", grid)
    with duckdb.connect() as db:
        db.execute("CREATE TABLE events AS SELECT *, CAST(amount_raw AS DECIMAL(38,0)) AS raw FROM read_parquet(?)", [str(output / "events.parquet")])
        db.execute("CREATE TABLE grid AS SELECT * FROM read_parquet(?)", [str(output / "grid.parquet")])
        queries = {
            "edges": """SELECT date, token, from_category, to_category, treatment,
                        CAST(SUM(raw) AS VARCHAR) amount_raw, CAST(SUM(raw) AS DOUBLE)/1e6 amount, COUNT(*) transfer_count
                        FROM events GROUP BY ALL ORDER BY date, token, from_category, to_category, treatment""",
            "hourly": """WITH directed AS (
                        SELECT hour, token, to_category category, raw, 1 direction, from_address, to_address FROM events WHERE treatment='boundary'
                        UNION ALL SELECT hour, token, from_category category, raw, -1 direction, from_address, to_address FROM events WHERE treatment='boundary'),
                        sums AS (SELECT hour, token, category,
                        SUM(CASE WHEN direction=1 THEN raw ELSE 0 END) inflow_raw,
                        SUM(CASE WHEN direction=-1 THEN raw ELSE 0 END) outflow_raw,
                        COUNT(*) FILTER(WHERE direction=1) inflow_count, COUNT(*) FILTER(WHERE direction=-1) outflow_count,
                        COUNT(DISTINCT from_address) active_senders, COUNT(DISTINCT to_address) active_receivers
                        FROM directed GROUP BY ALL)
                        SELECT g.hour,g.token,g.category,g.month,
                        CAST(COALESCE(s.inflow_raw,0) AS VARCHAR) inflow_raw, CAST(COALESCE(s.outflow_raw,0) AS VARCHAR) outflow_raw,
                        CAST(COALESCE(s.inflow_raw,0) AS DOUBLE)/1e6 inflow, CAST(COALESCE(s.outflow_raw,0) AS DOUBLE)/1e6 outflow,
                        COALESCE(s.inflow_count,0) inflow_count, COALESCE(s.outflow_count,0) outflow_count,
                        COALESCE(s.active_senders,0) active_senders, COALESCE(s.active_receivers,0) active_receivers
                        FROM grid g LEFT JOIN sums s USING(hour,token,category) ORDER BY g.hour,g.token,g.category""",
            "totals": """WITH sums AS (SELECT hour,token,SUM(raw) amount_raw,COUNT(*) transfer_count,
                        SUM(CASE WHEN from_category!='unknown' OR to_category!='unknown' THEN raw ELSE 0 END) known_amount_raw
                        FROM events WHERE treatment NOT IN ('zero_address','self','zero_amount') GROUP BY ALL)
                        SELECT DISTINCT g.hour,g.token,g.month,CAST(COALESCE(s.amount_raw,0) AS VARCHAR) amount_raw,
                        CAST(COALESCE(s.amount_raw,0) AS DOUBLE)/1e6 amount,COALESCE(s.transfer_count,0) transfer_count,
                        CAST(COALESCE(s.known_amount_raw,0) AS VARCHAR) known_amount_raw,
                        CAST(COALESCE(s.known_amount_raw,0) AS DOUBLE)/1e6 known_amount
                        FROM grid g LEFT JOIN sums s USING(hour,token) ORDER BY g.hour,g.token""",
        }
        for name, query in queries.items():
            cursor = db.execute(query)
            fields = [entry[0] for entry in cursor.description]
            rows = [dict(zip(fields, values)) for values in cursor.fetchall()]
            if name in ("hourly", "totals"):
                for row in rows:
                    complete = row.pop("month") in complete_months
                    row["complete"] = complete
                    if not complete:
                        for key in row:
                            if key not in ("hour", "token", "category", "complete"):
                                row[key] = None
            write_parquet(output / f"{name}.parquet", rows)
    (output / "grid.parquet").unlink()
    return finalize_snapshot(output, {"kind": kind, "start": start, "end": end, "categories": categories,
                                      "complete_months": complete_months, "source": source,
                                      "duplicates_removed": duplicates, "historical_labels": "fixed-label replay where validity is unknown"})


def analyze(snapshot: Path) -> dict:
    manifest, hourly = load_snapshot(snapshot)
    edges = read_parquet(snapshot / "edges.parquet")
    total_rows = read_parquet(snapshot / "totals.parquet")
    groups = defaultdict(lambda: {"amount_raw": 0, "known_amount_raw": 0, "transfer_count": 0, "complete": True})
    for row in total_rows:
        date = str(row["hour"])[:10]
        group = groups[(date, row["token"])]
        if row["complete"]:
            group["amount_raw"] += int(row["amount_raw"])
            group["known_amount_raw"] += int(row["known_amount_raw"])
            group["transfer_count"] += int(row["transfer_count"])
        else:
            group["complete"] = False
    totals = []
    for (date, token), group in sorted(groups.items()):
        amount = group["amount_raw"] / 1e6 if group["complete"] else None
        known = group["known_amount_raw"] / 1e6 if group["complete"] else None
        totals.append({"date": date, "token": token, **group, "amount": amount, "known_amount": known,
                       "coverage": known / amount if amount else None})
    edge_sums=defaultdict(int)
    for row in edges:
        if row['treatment'] not in ('zero_address','self','zero_amount'):
            edge_sums[(str(row['date'])[:10],row['token'])]+=int(row['amount_raw'])
    reconciled_days=0
    for row in totals:
        if row['complete']:
            if row['known_amount_raw']<0 or row['known_amount_raw']>row['amount_raw']:
                raise ValueError('Known endpoint amount exceeds eligible transfer amount')
            if edge_sums[(row['date'],row['token'])]!=row['amount_raw']:
                raise ValueError('Flow matrix does not reconcile with complete-day transfer totals')
            reconciled_days+=1
    incomplete = sum(not row["complete"] for row in hourly)
    quality = {"matrix_reconciled": not incomplete and all(r['complete'] for r in total_rows), "reconciled_complete_token_days":reconciled_days,
               "incomplete_hourly_rows": incomplete,
               "duplicates_removed": manifest.get("duplicates_removed", 0), "categories": manifest["categories"],
               "uncovered_categories": [c for c in CATEGORIES if c not in manifest["categories"]],
               "evidence_kind": manifest["kind"], "amount_unit": "native token units; not USD",
               "events_are_users": False, "historical_label_scope": manifest.get("historical_labels", "fixed-label historical replay")}
    findings = []
    if manifest["kind"] == "real":
        for token in TOKENS:
            selected = [row for row in totals if row["token"] == token and row["complete"]]
            amount = sum(row["amount"] for row in selected)
            known = sum(row["known_amount"] for row in selected)
            if amount:
                findings.append(f"{token}: {known/amount:.1%} of eligible transfer value touches at least one verified endpoint; remaining value has two unknown endpoints.")
    else:
        findings.append("Synthetic test data validates the software pipeline; it establishes no on-chain research finding.")
    return {"manifest": manifest, "totals": totals, "edges": edges, "hourly": hourly, "quality": quality, "findings": findings}
