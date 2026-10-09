-- migrations/001_create_accounts.sql
-- Runs as the owner role. The app role owns nothing and gets only the grants below.

CREATE TABLE accounts
(
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name           VARCHAR     NOT NULL,
    currency       VARCHAR     NOT NULL,
    allow_negative BOOLEAN,
    created_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT accounts_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT accounts_name_length CHECK (char_length(name) BETWEEN 1 AND 100 AND label = btrim(label, E' \t\r\n')),

    CONSTRAINT accounts_name_key UNIQUE (name),

    CONSTRAINT accounts_id_currency_key UNIQUE (id, currency),
    CONSTRAINT accounts_id_allow_negative_key UNIQUE (id, allow_negative)
);

CREATE INDEX accounts_currency_id_idx ON accounts(currency, id);
CREATE INDEX accounts_allow_negative_id ON accounts(allow_negative, id);

GRANT SELECT, INSERT ON accounts TO ledger_app;