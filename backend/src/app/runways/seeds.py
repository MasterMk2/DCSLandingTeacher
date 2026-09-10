"""Resolve configured runway seed geometry.

A sweep can only happen while the map is loaded on a DCS server (the terrain
files themselves are encrypted, and DCSServerBot's ``/airbase`` runs Lua in the
running mission), so a theatre nobody is flying right now cannot be captured on
demand. The way an import of an old recording still resolves is that the sweep
was captured *once*, committed, and shipped.

Two directories, in priority order:

- the writable cache (``DLT_RUNWAY_CACHE_DIR``), where live sweeps are stored;
- the configured, read-only seed directory.

Git holds the canonical copy in ``config/runways/``. The deployment mounts
``config/`` at ``/app/config`` rather than placing data in a Python package.
"""

from __future__ import annotations

from pathlib import Path


def resolve_seed_dir(configured: str | Path | None) -> Path | None:
    """Configured runway seed directory, or ``None`` when it is unavailable."""
    if configured:
        target = Path(configured)
        if target.is_dir():
            return target
    return None
