"""Size cap on request bodies other than ACMI uploads.

FastAPI reads a JSON body into memory and parses it before the endpoint or
its auth dependency runs. Unless the cap is enforced while the body arrives,
any client -- one without the token included -- can make the server hold a
body of any size before it is answered.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import yaml

from app.api.main import create_app
from tests.conftest import GRADING_YAML
from tests.helpers import make_api_settings, open_api_client

_CHUNK = 16 * 1024
_REGRADE = "/api/v1/landings/1/regrade"


class _StreamedJson:
    """A JSON body sent in chunks, counting what the server pulled from it."""

    parts: list[bytes]
    total: int
    sent: int

    def __init__(self, size: int) -> None:
        head = b'{"overrides": {"padding": "'
        tail = b'"}}'
        payload = head + b"x" * (size - len(head) - len(tail)) + tail
        self.parts = [payload[i : i + _CHUNK] for i in range(0, len(payload), _CHUNK)]
        self.total = len(payload)
        self.sent = 0

    async def stream(self) -> AsyncIterator[bytes]:
        for part in self.parts:
            self.sent += len(part)
            yield part

    async def post(self, http: httpx2.AsyncClient, **headers: str) -> httpx2.Response:
        return await http.post(
            _REGRADE,
            content=self.stream(),
            headers={"Content-Type": "application/json", **headers},
        )


async def test_a_chunked_json_body_is_cut_off_at_the_limit_before_authentication(
    tmp_path: Path,
) -> None:
    """No Content-Length and no token: the server stops reading at the cap
    and answers 413, instead of buffering all 8 MB and then answering 401."""
    app = create_app(
        make_api_settings(
            tmp_path, database_filename="limit.db", auth_token="secret", api_max_body_kb=64
        )
    )
    body = _StreamedJson(8 * 1024 * 1024)
    async with app.router.lifespan_context(app):
        async with open_api_client(app) as http:
            response = await body.post(http)

    assert body.sent <= 64 * 1024 + 2 * _CHUNK < body.total
    assert response.status_code == 413
    assert response.json()["error"] == "REQUEST_TOO_LARGE"
    assert response.json()["details"] == {"max_body_kb": 64}


async def test_a_declared_length_over_the_limit_is_refused_unread(tmp_path: Path) -> None:
    app = create_app(
        make_api_settings(tmp_path, database_filename="limit.db", api_max_body_kb=64)
    )
    body = _StreamedJson(64 * 1024 + 1)
    async with app.router.lifespan_context(app):
        async with open_api_client(app) as http:
            response = await body.post(http, **{"Content-Length": str(body.total)})

    assert body.sent == 0
    assert response.status_code == 413
    assert response.json()["error"] == "REQUEST_TOO_LARGE"


async def test_the_whole_grading_config_fits_under_the_default_limit(tmp_path: Path) -> None:
    """The largest body the API has a use for is a regrade whose overrides
    are the entire grading config. The default cap has to let it through."""
    overrides = yaml.safe_load(GRADING_YAML.read_text(encoding="utf-8"))
    # Indented and ASCII-escaped: the largest way a client would encode it.
    content = json.dumps({"overrides": overrides}, indent=2).encode()
    app = create_app(make_api_settings(tmp_path, database_filename="limit.db"))
    async with app.router.lifespan_context(app):
        async with open_api_client(app) as http:
            response = await http.post(
                "/api/v1/landings/999/regrade",
                content=content,
                headers={"Content-Type": "application/json"},
            )

    # Past the cap and the body parser into the endpoint, which finds no
    # such landing.
    assert response.status_code == 404
    assert response.json()["detail"] == "landing not found"
