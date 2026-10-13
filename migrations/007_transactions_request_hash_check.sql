-- migrations/007_transactions_request_hash_check.sql
-- Run as the owner role. App role gets only specified grants.

-- a request_hash should be a SHA256 HEX hash 64 characters long
ALTER TABLE transactions ADD CONSTRAINT transactions_request_hash_format CHECK (
    request_hash ~ '^[0-9a-f]{64}$'
);
