-- Creates the database the test suite runs against, set up like the service's own database.
-- Every test run empties its tables, so it must not be the database a developer is using through
-- the running service. Included by roles.sql on a fresh volume; run it by hand once on a volume
-- that already exists (see "Running tests" in the README). Required psql variable: testdbname.
-- Needs the roles from roles.sql, and runs as the superuser.

CREATE DATABASE :"testdbname" OWNER ledger_owner;

REVOKE ALL ON DATABASE :"testdbname" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"testdbname" TO ledger_owner, ledger_app;

\connect :"testdbname"

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO ledger_app;
