"""Application settings loaded from environment variables / .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DLT_", env_file=".env", extra="ignore")

    # HTTP API server
    host: str = "0.0.0.0"
    port: int = 8000

    # Database (SQLite via aiosqlite)
    database_url: str = "sqlite+aiosqlite:///./data/dlt.db"

    # Tacview realtime telemetry stream (ACMI 2.2 Text over TCP)
    tacview_host: str = "127.0.0.1"
    tacview_port: int = 31010

    # Real-Time Telemetry handshake identity (client side).
    # Leave tacview_password empty when the session is unprotected.
    tacview_client_name: str = "DCSLandingTeacher"
    tacview_password: str = ""

    # Disable to run API-only without the background ACMI client
    acmi_enabled: bool = True

    # Automatic reconnection backoff
    reconnect_initial_delay: float = 1.0
    reconnect_max_delay: float = 30.0

    # Grading thresholds (YAML); relative to the working directory.
    grading_config_path: str = "config/grading.yaml"

    # Per-carrier FLOLS geometry (Issue #3). Values are unverified
    # estimates; see the comments in config/carriers.yaml.
    carriers_config_path: str = "config/carriers.yaml"

    # Apply Alembic migrations automatically at startup (Issue #7). When
    # disabled, the legacy create_all bootstrap is used instead (dev mode).
    migrations_on_startup: bool = True

    # Explicit path to the Alembic migrations directory. Empty means
    # auto-detect next to the app package. Set this in containers where the
    # package is installed into site-packages (e.g. /app/migrations).
    migrations_dir: str = ""

    # Simple shared-token authentication (Issue #8). Empty (default) disables
    # authentication entirely and the API behaves exactly as before. When set,
    # REST endpoints under /api require "Authorization: Bearer <token>" or
    # "X-Auth-Token"; the WebSocket accepts "?token=<token>". /api/health and
    # the SPA static hosting stay public.
    auth_token: str = ""

    # CORS origins allowed to call the API from a browser. Empty list means
    # same-origin only (no CORS headers are emitted), which is the default
    # single-container deployment where the frontend is served by this app.
    # Example: DLT_CORS_ORIGINS=["http://localhost:5173","https://dlt.example.com"]
    cors_origins: list[str] = []

    # Built frontend directory served in production. When the directory does
    # not exist the API runs without static file hosting (dev mode).
    frontend_dist_dir: str = "frontend/dist"


def get_settings() -> Settings:
    return Settings()
