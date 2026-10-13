-- migrations/007_transactions_request_hash_check.sql
-- Run as the owner role. App role gets only specified grants.

ALTER TABLE transactions ADD CONSTRAINT transactions_request_hash_format CHECK (
    request_hash ~ '^[0-9a-f]{64}$'
);
