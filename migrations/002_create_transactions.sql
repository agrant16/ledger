-- migrations/002_create_transactions.sql
-- Run as the owner role. App role gets only specified grants.

CREATE TABLE transactions
(
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key VARCHAR UNIQUE NOT NULL,
    request_hash VARCHAR NOT NULL,
    reverses_transaction_id BIGINT UNIQUE REFERENCES transactions (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT transactions_idempotency_key_length CHECK (
        char_length(idempotency_key) BETWEEN 1 AND 255
    )
);

GRANT SELECT, INSERT ON transactions TO ledger_app;
