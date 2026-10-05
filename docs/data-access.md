# Data access and evidence

Real source data has **not** been fetched in this checkout: no Google project or credentials were supplied. The synthetic test preview makes no Ethereum research claims. Address evidence is real: the repository preserves official Aave, Arbitrum, issuer, and Uniswap documents and genuine block-pinned public Ethereum read calls in `data/evidence/`.

## Automatic acquisition

Create a separate **unbilled BigQuery Sandbox** project. Do not attach a billing account. Set `project_id` in `configs/ethereum.json`, install the optional fetch dependencies, and authenticate outside the repository:

```powershell
python -m pip install -e ".[fetch]"
gcloud auth application-default login
python -m stableflow fetch --config configs/ethereum.json --output artifacts/ethereum
```

The implementation reads actual table metadata before generating queries. Amount, address and event columns must be unambiguous scalar fields. `schema_map` can disambiguate a verified field (for example `{"amount": "quantity"}` **only when the actual metadata confirms quantity**). Arrays, floating-point amounts, unknown schemas, hexadecimal amounts and integer overflow fail closed. No guessed schema is shipped as if it had been observed.

The [Google schema page](https://docs.cloud.google.com/blockchain-analytics/docs/schema) documents monthly `block_timestamp` partitions and directs readers to the table metadata for exact columns. This implementation queries metadata for the modern `goog_blockchain_ethereum_mainnet_us` dataset, not the unrelated legacy `crypto_ethereum` layout.

Every month is dry-run before the first live submission. Acquisition is limited to 600 GiB from an 800-GiB project cap, retaining 200 GiB for verification. If all twelve months exceed the remaining acquisition budget, the same preflight evaluates only the final six complete months. If those also exceed it, **no live query is submitted**. A query's `maximum_bytes_billed` equals its reserved dry-run estimate, with a conservative minimum per referenced table. Changing table size can therefore fail a query rather than spending the reserve.

`.stableflow-budget.json` records actual billed bytes and conservatively retains reserved bytes when submission/failure is uncertain. Accounting uses the **UTC calendar month in which a job is submitted**, not the historical month queried. Fixed query job IDs and disabled job retries prevent a fresh retry job from escaping the reservation. A process lock prevents concurrent fetches from racing. Do not delete or reset the ledger to bypass a quota; if a process crashes, inspect its recorded Google job before removing its lock. The ledger covers StableFlow jobs only: use a dedicated project, and record any external usage before relying on the remaining budget. The program does not enable billing or provision a Google project, and does not independently assert that an existing project has billing disabled.

Google's Sandbox quota and availability remain subject to its [current documentation](https://docs.cloud.google.com/bigquery/docs/sandbox). Live schema, scan sizes and access must still be verified on the supplied account. The fetched snapshot preserves the actual schema, SQL, job IDs, raw query rows, label hash, source-evidence archive, coverage observations and Parquet checksums.

## Manual SQL and CSV route

This route uses the BigQuery console; Python needs no cloud credentials.

1. Run `python -m stableflow export-sql --config configs/ethereum.json --output sql/generated`. Until the schema is supplied it emits only `metadata.sql`, with `status: schema-required`.
2. Run that metadata query in the unbilled project; download its result as JSON, such as `configs/observed-schema.json`. Set `schema_file` to `observed-schema.json` in the config. Export SQL again. It now emits one bounded query per month plus `export.json` containing query and label hashes.
3. Use the BigQuery console's estimate and maximum-bytes-billed setting before each query; the manual route cannot control console submissions. Respect the same 600/200-GiB budget. If needed, set the research interval to the last six complete months **before** running any query. Save each union query result as `YYYY-MM.csv`, with exactly two columns: `row_type,payload`.
4. Save `provenance.json` beside those CSVs. Use the actual query job ID, actual completed state, CSV SHA-256 and exact SQL SHA-256 from `export.json`; never mark a failed job complete.

```json
{
  "dataset": "bigquery-public-data.goog_blockchain_ethereum_mainnet_us",
  "start": "2025-10-01T00:00:00Z",
  "end": "2026-10-01T00:00:00Z",
  "labels_sha256": "COPY_ACTUAL_EXPORT_LABEL_DIGEST",
  "schema": {"token_transfers": ["REPLACE_WITH_OBSERVED_FIELD_OBJECTS"]},
  "months": {
    "2025-10": {
      "job_id": "COPY_ACTUAL_BIGQUERY_JOB_ID",
      "status": "complete",
      "sha256": "COPY_CSV_SHA256",
      "sql_sha256": "COPY_EXACT_EXPORTED_SQL_SHA256"
    }
  }
}
```

The example is a structural guide, **not usable provenance or a claimed schema**. Supply every actual observed field object (name/type/mode, or column_name/data_type). Compute a file digest in PowerShell with `Get-FileHash -Algorithm SHA256 -LiteralPath path-to-file`, and use its lowercase value. Import with:

```powershell
python -m stableflow import-csv --config configs/ethereum.json --input downloads --output artifacts/ethereum
```

Missing/failed months remain incomplete with null hourly values. Repeated identical CSV aggregate rows are deduplicated; conflicting duplicate rows are rejected. Raw native amounts remain decimal integer strings. CSV imports are integrity-checked exports, not independently authenticated Google attestations: provenance authors remain responsible for truthful job information. Snapshot reuse requires a new output directory rather than overwriting a finalized artifact.

## Coverage and accounting

The source SQL deduplicates transaction/log events and raises on conflicting duplicated values. It checks that amounts fit exact `BIGNUMERIC` integers rather than truncating or dropping bad values. Endpoint classification happens before directional aggregation. Treatment precedence is zero address, self, zero amount, known same entity, same category, category boundary. All treatments remain in the daily matrix; ordinary totals exclude zero address, self and zero amount. Both tokens are native six-decimal units, never assumed dollars.

An hour may be zero-filled only when its monthly token query succeeded and its block observations have contiguous block numbers plus blocks within 120 seconds of both hour boundaries. Cross-hour and cross-month block-number gaps invalidate both affected hours. Missing observations are null. This detects measurable block-table gaps, but a successful token scan plus block continuity **cannot prove indexer completeness**; it is not a guarantee that all source tokens were indexed. The manifest states this limitation. Failed data queries never mark their month complete; if earlier months succeeded, they are preserved in an explicitly incomplete snapshot.

## Labels

- **Lending:** Aave V3 Ethereum USDC/USDT aToken custody addresses from the [official address book](https://github.com/bgd-labs/aave-address-book/blob/main/src/AaveV3Ethereum.sol), corroborated by `AToken.transferUnderlyingTo` source and actual `UNDERLYING_ASSET_ADDRESS()` reads. Lending transfers track asset custody and do not equal total loan origination.
- **Bridge:** Ethereum-side Arbitrum One ERC-20 and custom gateways from the [official deployment table](https://docs.arbitrum.io/arbitrum-essentials/reference/contract-addresses). The analysis describes only Ethereum-side movements.
- **DEX:** All nonzero pools returned by the official Ethereum Uniswap V3 factory for USDC/USDT, USDC/WETH and USDT/WETH, for fee tiers 100, 500, 3000 and 10000. Exact request/response bodies and pinned blocks are preserved. [Official factory deployment and discovery instructions](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-ethereum-deployments) support the method. Pool transfers may include liquidity/flash activity and are not equivalent to swap volume.
- **CEX:** Uncovered. No Binance address is inferred from explorer tags or memory. Add only a dated official public disclosure with preserved evidence.

Null label validity means fixed-label historical replay; addresses seen in current disclosures are not asserted to have been identifiable at every historical hour. Label conflicts become Unknown. Source-document licenses remain applicable; `AaveV3Ethereum.sol` is MIT, `AToken.sol` preserves its BUSL-1.1 header, and source documents are retained for attribution and evidence, independently from the project's code license. `sources.json` records their SHA-256 values.

## Raw verification and anomalous-event follow-up

The automatic fetch attempts bounded raw-log and receipt checks for samples at the beginning and end of the selected interval using the verification reserve. It compares the token log address, Transfer topic, sender/receiver topics, raw hex amount and successful receipt. Deferred or unsupported checks are recorded explicitly; they are not silently called passed.

`stableflow.fetch.fetch_evidence(config_path, snapshot, windows, output)` accepts one to twenty windows, each containing `id`, `start`, exclusive `end`, `token`, and covered `category`. Each window is at most 31 days and within the frozen snapshot. Its bounded SQL selects up to five major boundary transfers and corroborates them against logs/receipts. It dry-runs all selected events before submission, consumes the same ledger's verification budget and writes an attachment (`investigations.json` plus SQL); it does not mutate the frozen snapshot. Per transfer, `log_receipt_verified` and `verification_errors` state what the raw data supports.

Initial `evidence.json` samples are top transfers per month, not an exhaustive anomaly investigation. Events remain `temporarily unexplained` until independently corroborated. A successful raw Transfer check alone does not establish a business explanation or anomaly-detection accuracy. The follow-up method is available as a Python API; real conclusions and the top-twenty research review require the actual fetched data.
