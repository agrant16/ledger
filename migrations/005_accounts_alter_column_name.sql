-- "name" is a postgres keyword. Change to account_name.
ALTER TABLE accounts
RENAME COLUMN name TO account_name;

ALTER TABLE accounts
RENAME CONSTRAINT accounts_name_key
TO accounts_account_name_key;

ALTER TABLE accounts
RENAME CONSTRAINT accounts_name_length
TO accounts_account_name_length;

ALTER TABLE accounts
RENAME CONSTRAINT accounts_name_whitespace
TO accounts_account_name_whitespace;

ALTER TABLE accounts R
ENAME CONSTRAINT accounts_name_not_null
TO accounts_account_name_not_null;
