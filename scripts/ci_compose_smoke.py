"""Exercise a disposable GitHub Actions Compose stack; never use an operator DB."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def validate_environment(root: Path, env: dict[str, str]) -> str:
    """Require a unique CI project and refuse existing local configuration."""
    run = env.get("GITHUB_RUN_ID", "")
    attempt = env.get("GITHUB_RUN_ATTEMPT", "")
    if env.get("GITHUB_ACTIONS") != "true" or not run.isdecimal() or not attempt.isdecimal():
        raise RuntimeError("This smoke check runs only in an isolated GitHub Actions job")
    if env.get("DOCKER_HOST") or env.get("DOCKER_CONTEXT"):
        raise RuntimeError("Remote or custom Docker contexts are not supported")
    project = f"dlt-ci-{run}-{attempt}"
    if env.get("COMPOSE_PROJECT_NAME") != project:
        raise RuntimeError("The isolated Compose project name does not match this run")
    if (root / ".env").exists():
        raise RuntimeError("Refusing to replace existing environment configuration")
    if env.get("DLT_HOST") != "127.0.0.1" or env.get("DLT_PORT") != "18080":
        raise RuntimeError("The smoke listener must use the fixed loopback-only CI address")
    return project


def read_exact(stream, size: int) -> bytes:
    value = stream.read(size)
    if len(value) != size:
        raise AssertionError("Incomplete WebSocket response")
    return value


def websocket_ping(port: int) -> None:
    """Check the reverse proxy's Upgrade path and an application-level round trip."""
    key = base64.b64encode(b"dlt-ci-smoke-key").decode()
    expected = base64.b64encode(hashlib.sha1(
        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
    ).digest()).decode()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall((
            "GET /api/v1/ws/landings HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        with sock.makefile("rb") as stream:
            assert b" 101 " in stream.readline(4096), "WebSocket upgrade failed"
            headers: dict[str, str] = {}
            for _ in range(50):
                line = stream.readline(4096)
                if line == b"\r\n":
                    break
                name, value = line.decode().split(":", 1)
                headers[name.lower()] = value.strip()
            assert headers.get("sec-websocket-accept") == expected
            mask = b"test"
            payload = b"ping"
            sock.sendall(b"\x81\x84" + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))
            first, length = read_exact(stream, 2)
            assert first == 0x81 and length < 126, "Expected a short unmasked text response"
            assert json.loads(read_exact(stream, length)) == {"type": "pong"}


def wait_for_health(port: int) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=3) as response:
                assert response.status == 200
                json.load(response)
            return
        except (URLError, TimeoutError, OSError):
            time.sleep(1)
    raise RuntimeError("The isolated Compose stack did not become healthy")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    project = validate_environment(root, dict(os.environ))
    command = ["docker", "compose", "--project-name", project, "--file", str(root / "docker-compose.yml")]

    def compose(*args: str) -> None:
        subprocess.run([*command, *args], cwd=root, check=True, timeout=180)

    config = root / ".env"
    # No telemetry connection, no real data. The stale URL must be overridden by Compose.
    config.write_text("DLT_ACMI_ENABLED=false\nDLT_DATABASE_URL=postgresql+psycopg://stale@127.0.0.1:9/stale\n")
    port = 18080
    try:
        compose("up", "-d")
        wait_for_health(port)
        with urlopen(f"http://127.0.0.1:{port}/landings", timeout=5) as response:
            assert b'id="root"' in response.read(), "SPA fallback was not served"
        with urlopen(f"http://127.0.0.1:{port}/api/v1/landings?limit=1", timeout=5) as response:
            assert response.status == 200
            json.load(response)
        websocket_ping(port)
        marker = "ci-cache-" + project
        compose("exec", "-T", "api", "python", "-c",
                "from pathlib import Path; import os,sys; p=Path(os.environ['DLT_RUNWAY_CACHE_DIR']); "
                "p.mkdir(parents=True,exist_ok=True); (p/'.ci-smoke').write_text(sys.argv[1])", marker)
        compose("up", "-d", "--no-deps", "--force-recreate", "api", "reverse-proxy")
        wait_for_health(port)
        compose("exec", "-T", "api", "python", "-c",
                "from pathlib import Path; import os,sys; "
                "assert (Path(os.environ['DLT_RUNWAY_CACHE_DIR'])/'.ci-smoke').read_text()==sys.argv[1]", marker)
        websocket_ping(port)
        print("Compose smoke passed: migration, reserved-password DB, API, SPA, WebSocket, cache recreation")
    finally:
        try:
            # Only this unique disposable CI project's resources are removed.
            compose("down", "--volumes", "--remove-orphans")
        finally:
            config.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
