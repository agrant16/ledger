-- Creates the two database roles and fixes who owns what. Runs once, as the superuser,
-- before any migration. Locally it is invoked by docker/initdb/01-roles.sh; CI runs the
-- same file with psql. Required psql variables: dbname, owner_password, app_password.
--
-- ledger_owner: owns the database and, through it, every table the migrations create.
--               Migrations and test fixtures connect as this role.
-- ledger_app:   what the service connects as. Owns nothing. Each migration grants it
--               exactly the privileges it needs on the tables that migration creates.
--               There is deliberately no ALTER DEFAULT PRIVILEGES here or anywhere.

CREATE ROLE ledger_owner LOGIN PASSWORD :'owner_password';
CREATE ROLE ledger_app LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- On PostgreSQL 15+ the public schema is owned by pg_database_owner, i.e. the owner of
-- the current database, so this also makes ledger_owner the schema owner.
ALTER DATABASE :"dbname" OWNER TO ledger_owner;

REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"dbname" TO ledger_owner, ledger_app;

-- The app role may look things up in the schema but not create anything in it.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO ledger_app;
