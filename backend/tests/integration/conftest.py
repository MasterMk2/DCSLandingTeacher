"""Require migration-backed PostgreSQL schemas for integration scenarios."""

import pytest


@pytest.fixture(autouse=True)
def postgres_integration(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if not database_url.startswith("postgresql"):
        pytest.skip("DLT_TEST_POSTGRES_URL is required for backend integration tests")
    monkeypatch.setenv("DLT_TEST_ACTIVE_POSTGRES_URL", database_url)
