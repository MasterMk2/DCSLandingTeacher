"""Application settings loaded from environment variables / .env file."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TacviewSource(BaseModel):
    """Configuration for a single Tacview Real-Time Telemetry source."""

    id: str = Field(..., description="Unique source identifier (e.g., 'server1', 'caucasus-main')")
    name: str = Field(..., description="Display name (e.g., 'Caucasus Main', 'NTTR Training')")
    host: str = Field(default="127.0.0.1", description="Tacview server host/IP")
    port: int = Field(default=31010, description="Tacview server port")
    password: str = Field(default="", description="Handshake password (empty if unprotected)")
    client_name: str = Field(default="DCSLandingTeacher", description="Client name for handshake")
    idle_timeout: float = Field(default=60.0, description="Idle timeout in seconds (0 to disable)")
    enabled: bool = Field(default=True, description="Whether this source is enabled")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DLT_", env_file=".env", extra="ignore")

    # HTTP API server
    host: str = "0.0.0.0"
    port: int = 8000

    # Database (SQLite via aiosqlite)
    database_url: str = "sqlite+aiosqlite:///./data/dlt.db"

    # Tacview realtime telemetry stream (ACMI 2.2 Text over TCP)
    # Multi-source configuration (new): JSON array of TacviewSource objects.
    # Example: DLT_TACVIEW_SOURCES='[{"id":"s1","name":"Main","host":"10.0.0.1","port":31010}]'
    tacview_sources_json: str = Field(
        default="",
        description="JSON string of Tacview source configurations",
    )

    # Legacy single-source settings (used when tacview_sources_json is empty).
    tacview_host: str = "127.0.0.1"
    tacview_port: int = 31010
    tacview_client_name: str = "DCSLandingTeacher"
    tacview_password: str = ""

    # Default idle timeout for legacy single-source mode
    acmi_idle_timeout: float = 60.0

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

    # ACMI file import (POST /api/import): maximum accepted upload size in
    # megabytes. Larger uploads are rejected with 413.
    import_max_upload_mb: int = 200

    #: Uploaded recordings are scratch data kept out of the shared history.
    #: They are discarded explicitly by the UI, and any left behind (an
    #: abandoned tab, a restart) are swept once they are this old. 0 disables
    #: the sweep and keeps imports until they are discarded by hand.
    import_retention_hours: float = 24.0

    # --- Runway geometry from DCSServerBot's RestAPI ---------------------
    # Land landings are graded against the real runway (threshold position,
    # course, length) instead of guessing from the touchdown point. Leave
    # dcssb_base_url empty to disable and fall back to the touchdown-derived
    # approximation.
    dcssb_base_url: str = ""
    dcssb_api_prefix: str = "/stats"
    dcssb_api_key: str = ""
    #: Server name to query airbases for. Empty = use the first server the
    #: bot reports whose theatre matches the recording.
    dcssb_server_name: str = ""
    #: `/airbase` runs Lua on the DCS *simulation thread*, so a full sweep is
    #: paced. Do not lower this: it directly costs server frame time.
    dcssb_request_spacing_ms: int = 1500
    dcssb_timeout_s: float = 10.0
    #: Directory for the per-theatre runway cache (a sweep runs once per map).
    runway_cache_dir: str = "cache"

    @property
    def tacview_sources(self) -> list[TacviewSource]:
        """Return parsed list of Tacview sources.
        
        If tacview_sources_json is set, parse it. Otherwise fall back to
        legacy single-source configuration.
        """
        if self.tacview_sources_json:
            data = json.loads(self.tacview_sources_json)
            return [TacviewSource(**item) for item in data]
        # Backward compatibility: construct a single source from legacy settings
        return [TacviewSource(
            id="default",
            name="Default",
            host=self.tacview_host,
            port=self.tacview_port,
            password=self.tacview_password,
            client_name=self.tacview_client_name,
            idle_timeout=self.acmi_idle_timeout,
        )]

    @property
    def tacview_enabled(self) -> bool:
        """Return True if at least one source is enabled."""
        return any(s.enabled for s in self.tacview_sources)


def get_settings() -> Settings:
    return Settings()
