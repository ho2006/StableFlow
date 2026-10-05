-- Run in a separate, unbilled BigQuery Sandbox project and save results as JSON.
-- Source column metadata is authoritative. No amount field has been assumed.
SELECT table_name, column_name, data_type
FROM `bigquery-public-data.goog_blockchain_ethereum_mainnet_us.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name IN ('token_transfers', 'blocks', 'logs', 'receipts')
ORDER BY table_name, ordinal_position;
