"""Tacview connection settings passed to the source client."""

from app.config import Settings
from pathlib import Path
import pytest


def test_legacy_and_json_sources_use_configured_tacview_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DLT_TACVIEW_PORT", raising=False)
    monkeypatch.delenv("DLT_TACVIEW_SOURCES_JSON", raising=False)
    legacy = Settings().tacview_sources
    assert len(legacy) == 1
    assert legacy[0].port == 42674
    explicit = Settings(tacview_port=31010).tacview_sources
    assert explicit[0].port == 31010
    multi = Settings(
        tacview_sources_json='[{"id":"first","name":"First"},'
        '{"id":"second","name":"Second","port":31010}]',
    ).tacview_sources
    assert [source.port for source in multi] == [42674, 31010]
