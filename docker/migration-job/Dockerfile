FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend/src

COPY migration-job/pyproject.toml migration-job/uv.lock ./
COPY migration-job/alembic.ini ./
COPY migration-job/migrations ./migrations
COPY migration-job/src ./src
COPY backend/src /app/backend/src

RUN uv sync --no-dev --frozen

ENTRYPOINT ["uv", "run", "--no-dev", "migration-job"]
