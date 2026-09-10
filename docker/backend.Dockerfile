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
# The tuning YAMLs go INSIDE the package as well as into /app/config below.
# A bind mount can only shadow the path it is mounted on, and site-packages
# is not that path -- so this copy is readable however /app/config is
# mounted. It is what stops an empty config mount from silently demoting the
# server to the built-in code defaults (see app/grading/packaged.py); that
# is exactly what production was doing, with the LSO factor table missing
# and every carrier landing grading "OK" as a result.
#
# Copied at build time from the single canonical copy in config/, so the two
# cannot drift apart in git.
COPY config/grading.yaml config/carriers.yaml ./app/grading/defaults/
# Runway geometry captured from DCS, for the same reason and by the same route.
# A sweep needs the map loaded on a DCS server, so a theatre nobody is flying
# cannot be captured on demand -- these are the ones already captured, and they
# are what lets an import of an old recording resolve at all. Inside the
# package, where the empty /app/config mount cannot shadow them.
COPY config/runways/ ./app/runways/defaults/
RUN pip wheel --no-cache-dir --wheel-dir=/wheels .

# ---------------------------------------------------------------------------
# 3. Runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data /data/cache /app/config /app/frontend \
    && chown -R appuser:appuser /data /app

WORKDIR /app
COPY --from=backend-build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

COPY config/grading.yaml /app/config/grading.yaml
# carriers.yaml was never copied here, so even an image with a working config
# mount had no FLOLS geometry: every carrier approach fell back to the
# touchdown-referenced approximation.
COPY config/carriers.yaml /app/config/carriers.yaml
COPY --from=frontend-build /src/frontend/dist /app/frontend/dist
# Alembic migration scripts (applied automatically at startup).
COPY backend/migrations /app/migrations

# Container-specific defaults (override via environment / compose).
ENV DLT_HOST=0.0.0.0 \
    DLT_PORT=8000 \
    DLT_DATABASE_URL=sqlite+aiosqlite:////data/dlt.db \
    DLT_GRADING_CONFIG_PATH=/app/config/grading.yaml \
    DLT_CARRIERS_CONFIG_PATH=/app/config/carriers.yaml \
    DLT_FRONTEND_DIST_DIR=/app/frontend/dist \
    DLT_MIGRATIONS_DIR=/app/migrations \
    DLT_RUNWAY_CACHE_DIR=/data/cache

VOLUME ["/data"]
EXPOSE 8000

USER appuser
CMD ["uvicorn", "app.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
