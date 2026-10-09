-- migrations/003_create_entries.sql
-- Runs as the owner role. The app role owns nothing and gets only the grants below.

CREATE TABLE entries
(
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id BIGINT      NOT NULL,
    account_id     BIGINT      NOT NULL,
    amount_minor   BIGINT  NOT NULL,
    currency VARCHAR NOT NULL,

    CONSTRAINT entries_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT entries_amount_minor_not_zero CHECK (amount_minor <> 0),

    CONSTRAINT entries_accounts_currency_fkey
        FOREIGN KEY (account_id, currency)
        REFERENCES accounts (id, currency),

    CONSTRAINT entries_transaction_id_fky
        FOREIGN KEY (transaction_id)
        REFERENCES transactions (id)
);

CREATE INDEX entries_accounts_id ON entries (account_id, id);
CREATE INDEX entries_transaction_id ON entries (transaction_id);

GRANT SELECT, INSERT ON entries TO ledger_app;