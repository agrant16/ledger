# Starts the database, applies migrations, and runs the app with auto-reload on http://localhost:8000.
# Uses the default DATABASE_*_URL values from ledger.infra.config unless they are set in the environment.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

docker compose up -d --wait db
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$env:PYTHONPATH = "src"
uv run python -m ledger.migrate
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

uv run uvicorn ledger.main:app --reload --port 8000
