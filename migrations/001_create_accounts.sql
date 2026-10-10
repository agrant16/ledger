-- migrations/001_create_accounts.sql
-- Run as the owner role. App role gets only specified grants.

CREATE TABLE accounts
(
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name VARCHAR NOT NULL,
    currency VARCHAR NOT NULL,
    allow_negative BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT accounts_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT accounts_name_length CHECK (char_length(name) BETWEEN 1 AND 100),
    CONSTRAINT accounts_name_whitespace CHECK (
        name = btrim(name, E' \t\n\x0b\x0c\r')
    ),
    CONSTRAINT accounts_name_key UNIQUE (name),
    CONSTRAINT accounts_id_currency_key UNIQUE (id, currency),
    CONSTRAINT accounts_id_allow_negative_key UNIQUE (id, allow_negative)
);

GRANT SELECT, INSERT ON accounts TO ledger_app;
