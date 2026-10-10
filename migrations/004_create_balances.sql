-- Pre-check on migration for protected accounts (account.allow_negative = false) that would end in a negative balance
-- after the migration. Fails loudly with information about the account IDs and balances.
DO
$$
DECLARE
max_reported CONSTANT int := 20;
    bad_count bigint;
    bad_list text;
BEGIN
SELECT count(*),
       string_agg(failing.error_summary, ', ' ORDER BY failing.id)
           FILTER (WHERE failing.rn <= max_reported)
INTO bad_count, bad_list
FROM (SELECT a.id,
             -- Format the ID and balance together
             a.id || ' (' || e.total || ')' AS error_summary,
             row_number()                      OVER (ORDER BY a.id) AS rn
      FROM accounts a
               JOIN (
          -- Pull the total balance forward so it can be used in the message
          SELECT account_id, sum(amount_minor) AS total
          FROM entries
          GROUP BY account_id
          HAVING sum(amount_minor) < 0) e ON e.account_id = a.id
      WHERE NOT a.allow_negative) failing;

IF bad_count > max_reported THEN
    RAISE EXCEPTION
        'balances backfill: % protected account(s) would be negative; showing the first % [ID (Balance)]: %',
        bad_count, max_reported, bad_list;
ELSIF bad_count > 0 THEN
    RAISE EXCEPTION
        'balances backfill: % protected account(s) would be negative; [ID (Balance)]: %',
        bad_count, bad_list;
END IF;
END $$;

CREATE TABLE balances
(
    account_id     BIGINT PRIMARY KEY,
    allow_negative BOOLEAN NOT NULL,
    balance_minor  BIGINT  NOT NULL,

    CONSTRAINT balances_accounts_id_allow_negative_fkey
        FOREIGN KEY (account_id, allow_negative)
            REFERENCES accounts (id, allow_negative),

    CONSTRAINT balances_protected_nonnegative CHECK (allow_negative OR balance_minor >= 0)
);

INSERT INTO balances (account_id, allow_negative, balance_minor)
SELECT a.id                             AS account_id,
       a.allow_negative,
       COALESCE(sum(e.amount_minor), 0) AS balance_minor
FROM accounts a
         LEFT JOIN entries e
                   ON a.id = e.account_id
GROUP BY a.id, a.allow_negative;

GRANT SELECT, INSERT, UPDATE ON balances TO ledger_app;