"""Resolve host runway seeds before the copy shipped outside the config mount.

Exact captures take priority over live sweeps in the runway provider. Host
seed files take priority over their bundled copies, including partial mounts.
"""

from __future__ import annotations

from pathlib import Path
from logging import getLogger

from app.configuration_files import BUNDLED_CONFIG_DIR

logger = getLogger(__name__)
BUNDLED_SEED_DIR = BUNDLED_CONFIG_DIR / "runways"


def resolve_seed_dir(configured: str | Path | None) -> Path:
    """Return the existing configured directory or the bundled seed directory."""
    if configured:
        target = Path(configured)
        if target.is_dir():
            return target
    if not BUNDLED_SEED_DIR.is_dir():
        raise FileNotFoundError(f"runway seeds missing: {configured}; bundled copy missing: {BUNDLED_SEED_DIR}")
    logger.warning("runway seeds missing: %s; using bundled copy: %s", configured, BUNDLED_SEED_DIR)
    return BUNDLED_SEED_DIR
