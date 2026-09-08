FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_DEV=1 \
    UV_COMPILE_BYTECODE=1

# Install dependencies first for better Docker layer caching.
COPY backend/pyproject.toml backend/uv.lock ./

RUN uv sync \
    --frozen \
    --no-install-project \
    --no-editable

# Application source.
COPY backend/src ./src

# The tuning YAMLs also live inside the installed package.
# This packaged copy remains available even when /app/config is shadowed
# by a bind mount.
<<<<<<< HEAD
COPY config/grading.yaml config/carriers.yaml ./src/app/grading/defaults/
# Runway geometry is also available in the installed package when the DCS
# server cannot sweep a theatre on demand.
COPY config/runways/ ./src/app/runways/defaults/
=======
COPY config/grading.yaml config/carriers.yaml ./src/app/grading/defaults/
>>>>>>> 8bde5db (refactor: adopt backend src layout)

# Install the backend itself as a non-editable package.
RUN uv sync \
    --frozen \
    --no-editable


FROM python:3.11-slim-bookworm AS runner

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DLT_GRADING_CONFIG_PATH=/app/config/grading.yaml \
    DLT_CARRIERS_CONFIG_PATH=/app/config/carriers.yaml \
    DLT_RUNWAY_CACHE_DIR=/data/cache

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data /data/cache /app/config \
    && chown -R appuser:appuser /data /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv

COPY --chown=appuser:appuser config/grading.yaml /app/config/grading.yaml
COPY --chown=appuser:appuser config/carriers.yaml /app/config/carriers.yaml

VOLUME ["/data"]

USER appuser

EXPOSE 8000

USER appuser

CMD ["uvicorn", "app.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "9001"]
