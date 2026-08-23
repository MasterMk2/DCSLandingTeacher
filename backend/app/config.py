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


def get_settings() -> Settings:
    return Settings()
