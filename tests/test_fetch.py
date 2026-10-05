"""Acquisition checks without Google credentials or paid queries."""
import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stableflow.common import iso, read_json, read_parquet, sha256, utc, write_json
from stableflow.fetch import Budget, GIB, TRANSFER_TOPIC, _decode, _mapping, _snapshot, _verify_evidence, aggregate_sql, fetch, import_csv


FIELDS = [SimpleNamespace(name=n,field_type=t,mode="NULLABLE") for n,t in (
    ("block_timestamp","TIMESTAMP"),("token_address","STRING"),("from_address","STRING"),
    ("to_address","STRING"),("value","STRING"),("transaction_hash","STRING"),("log_index","INTEGER"),("block_number","INTEGER"))]


class Client:
    def __init__(self, estimate=10*1024**2, fail=False,fail_after=None):
        self.estimate, self.fail, self.fail_after = estimate,fail,fail_after
        self.dry,self.live = [],[]

    def get_table(self,name):
        return SimpleNamespace(schema=FIELDS)

    def query(self,sql,job_config,**kwargs):
        if job_config.dry_run:
            self.dry.append(sql)
            return SimpleNamespace(total_bytes_processed=self.estimate)
        self.live.append((sql,job_config.maximum_bytes_billed))
        if self.fail or (self.fail_after is not None and len(self.live)>self.fail_after):
            raise RuntimeError("mock cloud failure")
        start=sql.split("TIMESTAMP('",1)[1].split("'",1)[0]
        rows=[{"row_type":"coverage","payload":json.dumps({"hour":start,"block_count":300,"min_block":1,"max_block":300,"complete":True})}]
        return SimpleNamespace(job_id=kwargs.get("job_id",f"job-{len(self.live)}"),total_bytes_billed=self.estimate,result=lambda **kwargs:rows)


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.config={"dataset":"bigquery-public-data.goog_blockchain_ethereum_mainnet_us","start":"2025-01-01T00:00:00Z","end":"2025-02-01T00:00:00Z","labels_file":"labels.json","ledger_file":"budget.json","budget_gib":800,"reserve_gib":200,"schema_map":{}}
        self.labels=[{"address":"0x"+"1"*40,"entity":"test entity","category":"dex","source_url":"https://example.org/official","retrieved_at":"2026-10-04T00:00:00Z","valid_from":None,"valid_to":None,"verified":True,"evidence":"Unit test only"}]
        write_json(self.path/"labels.json",self.labels)
        self.save()
        self.patch=patch("stableflow.fetch._job_config",lambda dry,maximum=None:SimpleNamespace(dry_run=dry,maximum_bytes_billed=maximum))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def save(self):
        write_json(self.path/"config.json",self.config)

    def test_overbudget_never_submits(self):
        client=Client(estimate=601*GIB)
        with self.assertRaisesRegex(RuntimeError,"no live query"):
            fetch(self.path/"config.json",self.path/"snapshot",client=client)
        self.assertEqual(client.live,[])
        self.assertFalse((self.path/"snapshot"/"manifest.json").exists())

    def test_failure_reserved_and_no_complete_month(self):
        client=Client(fail=True)
        with self.assertRaisesRegex(RuntimeError,"mock cloud failure"):
            fetch(self.path/"config.json",self.path/"snapshot",client=client)
        ledger=read_json(self.path/"budget.json")
        self.assertEqual(ledger["queries"][0]["status"],"failed-or-uncertain")
        self.assertGreater(ledger["queries"][0]["bytes"],0)
        state=read_json(self.path/"snapshot"/"fetch-state.json")
        self.assertEqual(state["complete_months"],[])
        self.assertFalse((self.path/"snapshot"/"manifest.json").exists())

    def test_partial_success_preserved_without_failed_month_completion(self):
        self.config["end"]="2025-03-01T00:00:00Z"
        self.save()
        with self.assertRaisesRegex(RuntimeError,"mock cloud failure"):
            fetch(self.path/"config.json",self.path/"partial",client=Client(fail_after=1))
        manifest=read_json(self.path/"partial"/"manifest.json")
        self.assertEqual(manifest["complete_months"],["2025-01"])
        self.assertEqual(manifest["source"]["status"],"incomplete")
        rows=read_parquet(self.path/"partial"/"hourly.parquet")
        feb=[r for r in rows if str(r["hour"]).startswith("2025-02")]
        self.assertTrue(feb)
        self.assertFalse(any(r["complete"] for r in feb))
        self.assertTrue(all(r["inflow"] is None for r in feb))

    def test_fallback_happens_before_submission_and_gaps_stay_null(self):
        self.config.update(start="2025-01-01T00:00:00Z",end="2026-01-01T00:00:00Z")
        self.save()
        client=Client(estimate=60*GIB)
        manifest=fetch(self.path/"config.json",self.path/"snapshot",client=client)
        self.assertEqual(len(client.dry),18)
        self.assertEqual(len(client.live),6)
        self.assertEqual(manifest["start"],"2025-07-01T00:00:00Z")
        rows=read_parquet(self.path/"snapshot"/"hourly.parquet")
        first=next(r for r in rows if str(r["hour"]).startswith("2025-07-01 00:00:00") or r["hour"]=="2025-07-01T00:00:00Z")
        gap=next(r for r in rows if str(r["hour"]).startswith("2025-07-01 01:00:00") or r["hour"]=="2025-07-01T01:00:00Z")
        self.assertTrue(first["complete"])
        self.assertEqual(first["inflow"],0)
        self.assertFalse(gap["complete"])
        self.assertIsNone(gap["inflow"])

    def test_calendar_billing_month_not_research_month(self):
        month=datetime.now(timezone.utc).strftime("%Y-%m")
        write_json(self.path/"budget.json",{"version":1,"queries":[{"billing_month":"2001-01","phase":"data","bytes":799*GIB},{"billing_month":month,"phase":"data","bytes":599*GIB}]})
        config={**self.config,"ledger_file":str(self.path/"budget.json")}
        with Budget(config) as budget:
            self.assertEqual(budget.remaining(),GIB)
            self.assertEqual(budget.remaining("verification"),201*GIB)

    def test_integer_precision_and_duplicate_idempotence(self):
        row={"row_type":"totals","payload":json.dumps({"hour":"2025-01-01T00:00:00Z","token":"USDC","amount_raw":"9007199254740993","known_amount_raw":"1","transfer_count":1})}
        result=_decode([row,row])
        self.assertEqual(len(result["totals"]),1)
        self.assertEqual(result["totals"][0]["amount_raw"],"9007199254740993")
        bad={"row_type":"totals","payload":json.dumps({"hour":"2025-01-01T00:00:00Z","token":"USDC","amount_raw":"1.1","known_amount_raw":"1","transfer_count":1})}
        with self.assertRaisesRegex(ValueError,"exact nonnegative integer"):
            _decode([bad])

    def test_schema_rejects_float_and_ambiguous_amount(self):
        fields=[{"name":f.name,"type":f.field_type,"mode":f.mode} for f in FIELDS]
        fields.append({"name":"amount","type":"STRING"})
        with self.assertRaisesRegex(ValueError,"schema_map.amount"):
            _mapping(fields)
        fields=[{**f,"type":"FLOAT64"} if f["name"]=="value" else f for f in fields if f["name"]!="amount"]
        with self.assertRaisesRegex(ValueError,"imprecise"):
            _mapping(fields)

    def test_manual_csv_requires_checksums_and_preserves_missing_coverage(self):
        config={**self.config,"categories":["dex"],"labels_sha256":sha256(self.path/"labels.json")}
        schema=[{"name":f.name,"type":f.field_type,"mode":f.mode} for f in FIELDS]
        sql=aggregate_sql(config,self.labels,_mapping(schema),utc(config["start"]),utc(config["end"]))
        csv_path=self.path/"2025-01.csv"
        rows=[{"row_type":"coverage","payload":json.dumps({"hour":"2025-01-01T00:00:00Z","block_count":300,"min_block":1,"max_block":300,"complete":True})}]
        with csv_path.open("w",newline="",encoding="utf-8") as stream:
            writer=csv.DictWriter(stream,fieldnames=["row_type","payload"])
            writer.writeheader()
            writer.writerows(rows)
        import hashlib
        provenance={"dataset":config["dataset"],"labels_sha256":config["labels_sha256"],"schema":schema,"start":config["start"],"end":config["end"],"months":{"2025-01":{"job_id":"manual-job","status":"complete","sha256":sha256(csv_path),"sql_sha256":hashlib.sha256(sql.encode()).hexdigest()}}}
        write_json(self.path/"provenance.json",provenance)
        manifest=import_csv(self.path/"config.json",self.path,self.path/"imported")
        self.assertEqual(manifest["kind"],"real")
        self.assertGreater(manifest["missing_hours"],0)
        csv_path.write_text("broken",encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"checksum"):
            import_csv(self.path/"config.json",self.path,self.path/"other")

    def test_raw_log_and_receipt_verification(self):
        sender,receiver="0x"+"1"*40,"0x"+"2"*40
        event={"hour":"2025-01-01T00:00:00Z","token":"USDC","tx_hash":"0x"+"a"*64,"log_index":0,"block_number":1,"sender":sender,"receiver":receiver,"from_category":"unknown","to_category":"dex","treatment":"boundary","amount_raw":"1234567","log_address":"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48","log_data":"0x"+format(1234567,"064x"),"log_topics":[TRANSFER_TOPIC,"0x"+sender[2:].zfill(64),"0x"+receiver[2:].zfill(64)],"receipt_status":"1"}
        result=_verify_evidence([{"row_type":"evidence","payload":json.dumps(event)}])[0]
        self.assertTrue(result["log_receipt_verified"])
        event["receipt_status"]="0"
        result=_verify_evidence([{"row_type":"evidence","payload":json.dumps(event)}])[0]
        self.assertFalse(result["log_receipt_verified"])

    def test_cross_hour_block_gap_prevents_zero_fill_and_matrix_bounds_checked(self):
        config={**self.config,"categories":["dex"],"labels_sha256":sha256(self.path/"labels.json")}
        rows=[{"row_type":"coverage","payload":json.dumps({"hour":f"2025-01-01T0{i}:00:00Z","block_count":300,"min_block":1+i*301,"max_block":300+i*301,"complete":True})} for i in (0,1)]
        _snapshot(config,self.labels,rows,self.path/"gap",{},["2025-01"])
        hourly=read_parquet(self.path/"gap"/"hourly.parquet")
        self.assertFalse(any(r["complete"] for r in hourly))
        rows.append({"row_type":"edges","payload":json.dumps({"date":"2024-12-31","token":"USDC","from_category":"unknown","to_category":"dex","treatment":"boundary","amount_raw":"1","transfer_count":1})})
        with self.assertRaisesRegex(ValueError,"Matrix source date"):
            _snapshot(config,self.labels,rows,self.path/"wrongmonth",{},["2025-01"])


if __name__ == "__main__":
    unittest.main()
