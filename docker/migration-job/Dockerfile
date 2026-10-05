FROM python:3.11.15-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend/src

COPY migration-job/pyproject.toml migration-job/uv.lock ./
COPY migration-job/alembic.ini ./
COPY migration-job/migrations ./migrations
COPY migration-job/src ./src
COPY backend/src /app/backend/src

RUN uv lock --check && uv sync --no-dev --frozen

ENTRYPOINT ["uv", "run", "--no-sync", "--no-dev", "migration-job"]
