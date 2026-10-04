"""Lifecycle contracts for the disposable PostgreSQL test server."""

from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import make_url

from tests.postgres import postgres_test_server


def test_configured_postgres_url_does_not_start_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = Mock(side_effect=AssertionError("Docker must not be used"))
    monkeypatch.setattr("tests.postgres.subprocess.run", docker)
    url = "postgresql+psycopg://test@localhost/test"

    with postgres_test_server(url) as actual:
        assert actual == url

    docker.assert_not_called()


def test_configured_sqlite_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = Mock()
    monkeypatch.setattr("tests.postgres.subprocess.run", docker)

    with pytest.raises(ValueError, match="PostgreSQL"):
        with postgres_test_server("sqlite:///test.db"):
            pytest.fail("SQLite must not replace PostgreSQL")

    docker.assert_not_called()


def test_managed_server_is_removed_when_the_test_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        output = {"run": "container-id", "inspect": "healthy", "port": "127.0.0.1:55432"}
        return subprocess.CompletedProcess(command, 0, output.get(command[1], ""), "")

    monkeypatch.setattr("tests.postgres.subprocess.run", run)

    with pytest.raises(RuntimeError, match="test failed"):
        with postgres_test_server(None) as url:
            parsed = make_url(url)
            assert parsed.get_backend_name() == "postgresql"
            assert parsed.host == "127.0.0.1"
            assert parsed.port == 55432
            raise RuntimeError("test failed")

    created = next(command for command in commands if command[1] == "run")
    removed = next(command for command in commands if command[1] == "rm")
    assert created[created.index("--name") + 1] == removed[-1]
    assert "--volumes" in removed


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(1, "docker", stderr="private data"),
        subprocess.TimeoutExpired("docker", 120, stderr="private data"),
        FileNotFoundError("Docker executable is missing"),
    ],
)
def test_docker_failure_is_an_error_without_exposing_output(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    docker = Mock(side_effect=failure)
    monkeypatch.setattr("tests.postgres.subprocess.run", docker)

    with pytest.raises(RuntimeError, match="Docker") as error:
        with postgres_test_server(None):
            pytest.fail("Failed setup must not execute database tests")

    assert "private data" not in str(error.value)
    assert any(call.args[0][1] == "rm" for call in docker.call_args_list)


def test_unhealthy_server_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        output = "unhealthy" if command[1] == "inspect" else "container-id"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("tests.postgres.subprocess.run", run)

    with pytest.raises(RuntimeError, match="ready"):
        with postgres_test_server(None):
            pytest.fail("An unhealthy server must not execute database tests")

    assert commands[-1][1] == "rm"


def test_readiness_timeout_removes_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = Mock(return_value=subprocess.CompletedProcess(["docker"], 0, "container-id", ""))
    monkeypatch.setattr("tests.postgres.subprocess.run", docker)
    monkeypatch.setattr("tests.postgres.time.monotonic", Mock(side_effect=[0.0, 61.0]))

    with pytest.raises(RuntimeError, match="ready"):
        with postgres_test_server(None):
            pytest.fail("A server readiness timeout must fail setup")

    assert docker.call_args_list[-1].args[0][1] == "rm"
