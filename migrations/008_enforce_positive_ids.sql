-- migrations/enforce_positive_ids.sql
-- Run as the owner role. App role gets only specified grants.

-- Enforce auto-generated primary key ID > 0.
ALTER TABLE accounts ADD CONSTRAINT accounts_id_positive CHECK (id > 0);
ALTER TABLE transactions ADD CONSTRAINT transactions_id_positive CHECK (id > 0);
ALTER TABLE entries ADD CONSTRAINT entries_id_positive CHECK (id > 0);
