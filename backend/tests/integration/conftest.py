"""Require migration-backed PostgreSQL schemas for integration scenarios."""

import pytest


@pytest.fixture(autouse=True)
def postgres_integration(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLT_TEST_ACTIVE_POSTGRES_URL", database_url)
