-- Creates the two database roles and fixes who owns what, for the service's database and for a
-- separate database the test suite runs against. Runs once, as the superuser, before any
-- migration. Locally it is invoked by docker/initdb/01-roles.sh; CI runs the same file with psql.
-- Required psql variables: dbname, testdbname, owner_password, app_password.
--
-- ledger_owner: owns both databases and, through them, every table the migrations create.
--               Migrations and test fixtures connect as this role.
-- ledger_app:   what the service connects as. Owns nothing. Each migration grants it
--               exactly the privileges it needs on the tables that migration creates.
--               There is deliberately no ALTER DEFAULT PRIVILEGES here or anywhere.

CREATE ROLE ledger_owner LOGIN PASSWORD :'owner_password';
CREATE ROLE ledger_app LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- The service's database. On PostgreSQL 15+ the public schema is owned by pg_database_owner,
-- i.e. the owner of the current database, so this also makes ledger_owner the schema owner.
ALTER DATABASE :"dbname" OWNER TO ledger_owner;

REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"dbname" TO ledger_owner, ledger_app;

-- The app role may look things up in the schema but not create anything in it.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO ledger_app;

-- The database the test suite runs against (see test-database.sql).
\ir test-database.sql
