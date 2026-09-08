"""Where the shipped runway geometry lives.

A sweep can only happen while the map is loaded on a DCS server (the terrain
files themselves are encrypted, and DCSServerBot's ``/airbase`` runs Lua in the
running mission), so a theatre nobody is flying right now cannot be captured on
demand. The way an import of an old recording still resolves is that the sweep
was captured *once*, committed, and shipped.

Two directories, in priority order:

- the writable cache (``DLT_RUNWAY_CACHE_DIR``), where live sweeps are stored;
- these seeds, read-only, which the image carries.

The seeds go **inside the package**, exactly as the tuning YAMLs do and for the
same reason: production bind-mounts an empty host directory over ``/app/config``,
and a bind mount can only shadow the path it is mounted on. Site-packages is not
that path. Git holds one canonical copy in ``config/runways/``; the image build
stages it here, so the two cannot drift.
"""

from __future__ import annotations

from importlib import resources
from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

PACKAGED_ANCHOR = "app.runways"
PACKAGED_SUBDIR = "defaults"


def packaged_seed_dir() -> Path | None:
    """The copy shipped inside the package, or ``None`` in a source checkout."""
    try:
        candidate = resources.files(PACKAGED_ANCHOR).joinpath(PACKAGED_SUBDIR)
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    try:
        if not candidate.is_dir():
            return None
    except OSError:  # pragma: no cover - unusual loaders
        return None
    return Path(str(candidate))


def resolve_seed_dir(configured: str | Path | None) -> Path | None:
    """Directory to read shipped runway caches from, or ``None`` if there is none.

    The configured path wins when it exists -- that is the repository's
    ``config/runways/`` in a checkout. Otherwise the packaged copy, which is
    what a container actually reads.
    """
    if configured:
        target = Path(configured)
        if target.is_dir():
            return target
    return packaged_seed_dir()
