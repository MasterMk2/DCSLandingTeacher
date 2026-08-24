# syntax=docker/dockerfile:1
# Single-container production image: FastAPI backend serving the built
# frontend (SPA) plus the grading config. Build context must be the
# repository root:
#   docker build -f docker/backend.Dockerfile -t dcs-landing-teacher .

# ---------------------------------------------------------------------------
# 1. Frontend build (Vite + React + TypeScript)
# ---------------------------------------------------------------------------
FROM node:20-alpine AS frontend-build
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# 2. Backend wheel build
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS backend-build
WORKDIR /build
COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip wheel --no-cache-dir --wheel-dir=/wheels .

# ---------------------------------------------------------------------------
# 3. Runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data /app/config /app/frontend \
    && chown -R appuser:appuser /data /app

WORKDIR /app
COPY --from=backend-build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

COPY config/grading.yaml /app/config/grading.yaml
COPY --from=frontend-build /src/frontend/dist /app/frontend/dist
# Alembic migration scripts (applied automatically at startup).
COPY backend/migrations /app/migrations

# Container-specific defaults (override via environment / compose).
ENV DLT_HOST=0.0.0.0 \
    DLT_PORT=8000 \
    DLT_DATABASE_URL=sqlite+aiosqlite:////data/dlt.db \
    DLT_GRADING_CONFIG_PATH=/app/config/grading.yaml \
    DLT_FRONTEND_DIST_DIR=/app/frontend/dist \
    DLT_MIGRATIONS_DIR=/app/migrations

VOLUME ["/data"]
EXPOSE 8000

USER appuser
CMD ["uvicorn", "app.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
