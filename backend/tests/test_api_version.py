"""API versioning (Issue #38): routes are served under /api/v1 and the
unversioned /api alias is retained for backwards compatibility."""
from __future__ import annotations

from app.api.main import API_V1, API_VERSION


async def test_version_endpoint_under_v1(client):
    http, _ = client
    res = await http.get("/api/v1/version")
    assert res.status_code == 200
    assert res.json() == {"api": API_V1, "version": API_VERSION}


async def test_version_endpoint_under_legacy_alias(client):
    http, _ = client
    res = await http.get("/api/version")
    assert res.status_code == 200
    assert res.json()["api"] == API_V1


def _collect_paths(routes) -> set[str]:
    """Collect every route path, descending into FastAPI's _IncludedRouter
    wrappers (whose real routes live on ``original_router``)."""
    out: set[str] = set()
    for r in routes:
        path = getattr(r, "path", "")
        if path:
            out.add(path)
        nested = getattr(r, "routes", None)
        if nested:
            out |= _collect_paths(nested)
        original = getattr(r, "original_router", None)
        if original is not None:
            out |= _collect_paths(original.routes)
    return out


async def test_landings_routes_registered_under_v1_and_legacy(client):
    _, app = client
    # OpenAPI documents every REST route with its full (prefixed) path.
    paths = set(app.openapi()["paths"].keys())
    assert "/api/v1/landings" in paths
    assert "/api/landings" in paths
    assert "/api/v1/version" in paths

    # WebSocket routes are not in OpenAPI. FastAPI stores their path without
    # the include prefix (applied at match time), so we confirm the handler is
    # registered; the v1/legacy prefixes are applied by the same include loop
    # already verified for the REST routes above.
    all_paths = _collect_paths(app.router.routes)
    assert "/ws/landings" in all_paths
