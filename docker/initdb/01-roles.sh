#!/bin/sh
# Runs automatically on first start of an empty Postgres data directory.
set -eu

psql -v ON_ERROR_STOP=1 \
    -v dbname="$POSTGRES_DB" \
    -v owner_password="$LEDGER_OWNER_PASSWORD" \
    -v app_password="$LEDGER_APP_PASSWORD" \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -f /docker-roles/roles.sql
