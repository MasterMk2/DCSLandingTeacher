"""Compose DB components and native URL settings select the same database."""

from pathlib import Path
from typing import cast

import pytest
import yaml
from sqlalchemy.engine import make_url

from app.config import Settings


def test_compose_passes_database_components_without_building_a_raw_url() -> None:
    compose_path = Path(__file__).resolve().parents[4] / "docker-compose.yml"
    environment = cast(
        dict[str, str],
        yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]["api"]["environment"],
    )

    assert environment["DLT_DATABASE_URL"] == ""
    assert environment["DLT_POSTGRES_HOST"] == "db"
    assert environment["DLT_POSTGRES_PORT"] == "5432"
    for name in ("USER", "PASSWORD", "DB"):
        variable = f"DLT_POSTGRES_{name}"
        assert environment[variable] == f"${{{variable}:?{variable} must be set}}"


def test_compose_database_components_preserve_reserved_characters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    # Compose overrides an old .env URL with an empty value and supplies components.
    monkeypatch.setenv("DLT_DATABASE_URL", "")
    monkeypatch.setenv("DLT_POSTGRES_HOST", "db")
    monkeypatch.setenv("DLT_POSTGRES_PORT", "5432")
    monkeypatch.setenv("DLT_POSTGRES_USER", "test@user")
    monkeypatch.setenv("DLT_POSTGRES_PASSWORD", "test@pass/word:%#?+")
    monkeypatch.setenv("DLT_POSTGRES_DB", "dlt_test")

    url = make_url(Settings().database_url)

    assert url.drivername == "postgresql+psycopg"
    assert url.username == "test@user"
    assert url.password == "test@pass/word:%#?+"
    assert url.host == "db"
    assert url.port == 5432
    assert url.database == "dlt_test"


def test_explicit_native_database_url_takes_precedence_over_components(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    explicit = "postgresql+psycopg://native@localhost:55432/native_test"
    settings = Settings(
        database_url=explicit,
        postgres_host="db",
        postgres_user="compose_user",
        postgres_password="compose_password",
        postgres_db="compose_test",
    )
    assert settings.database_url == explicit
