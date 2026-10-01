"""Provision the PostgreSQL server used by backend database tests."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from uuid import uuid4

from sqlalchemy.engine import make_url


def _docker(*arguments: str, timeout: float = 30) -> str:
    try:
        result = subprocess.run(
            ["docker", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(
            "Docker could not prepare the test PostgreSQL server. " +
            "Start Docker or set DLT_TEST_POSTGRES_URL to a dedicated test database."
        ) from None
    return result.stdout.strip()


def _wait_until_ready(name: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = _docker("inspect", "--format", "{{.State.Health.Status}}", name)
        if status == "healthy":
            return
        if status == "unhealthy":
            break
        time.sleep(0.5)
    raise RuntimeError("The test PostgreSQL server did not become ready within 60 seconds.")


@contextmanager
def postgres_test_server(configured_url: str | None) -> Generator[str, None, None]:
    """Reuse a configured test server or own one disposable Docker server.

    Args:
        configured_url: Explicit PostgreSQL test URL, or None to provision a server.

    Yields:
        Connection URL for the test session. Callers isolate tests in separate schemas.
    """
    if configured_url:
        if make_url(configured_url).get_backend_name() != "postgresql":
            raise ValueError("DLT_TEST_POSTGRES_URL must specify PostgreSQL.")
        yield configured_url
        return

    name = f"dlt-backend-test-{uuid4().hex}"
    try:
        _ = _docker(
            "run", "--detach", "--rm", "--name", name,
            "--publish", "127.0.0.1::5432",
            "--env", "POSTGRES_USER=dlt_test",
            "--env", "POSTGRES_PASSWORD=test-password",
            "--env", "POSTGRES_DB=dlt_test",
            "--health-cmd", "pg_isready -h 127.0.0.1 -U dlt_test -d dlt_test",
            "--health-interval", "1s", "--health-timeout", "2s", "--health-retries", "60",
            "postgres:18", timeout=120,
        )
        _wait_until_ready(name)
        address = _docker("port", name, "5432/tcp")
        host, _, port = address.rpartition(":")
        if host != "127.0.0.1" or not port.isdecimal():
            raise RuntimeError("Docker did not publish the test PostgreSQL port on localhost.")
        url = f"postgresql+psycopg://dlt_test:test-password@127.0.0.1:{port}/dlt_test"
    except BaseException:
        # A failed or interrupted startup can still have created the container.
        with suppress(RuntimeError):
            _ = _docker("rm", "--force", "--volumes", name)
        raise

    try:
        yield url
    finally:
        _ = _docker("rm", "--force", "--volumes", name)
