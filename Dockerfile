FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src ./src
COPY migrations ./migrations

EXPOSE 8000
# Serves as the app role. Migrations run in the separate `migrate` Compose service, which
# overrides this command, so this process never holds the owner credentials.
CMD ["uvicorn", "ledger.main:app", "--host", "0.0.0.0", "--port", "8000"]
