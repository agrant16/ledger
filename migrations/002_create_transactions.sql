-- migrations/002_create_transactions.sql
-- Runs as the owner role. The app role owns nothing and gets only the grants below.

CREATE TABLE accounts
(
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key         VARCHAR     NOT NULL,
    request_hash            VARCHAR     NOT NULL,
    reverses_transaction_id BIGINT UNIQUE REFERENCES transactions (id),
    created_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT transactions_idempotency_key_length CHECK (char_length(name) BETWEEN 1 AND 255),
);

GRANT SELECT, INSERT ON transactions TO ledger_app;