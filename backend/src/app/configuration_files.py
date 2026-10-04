"""Resolve host configuration before the copies shipped with the application."""

from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)
_PACKAGED_CONFIG = Path(__file__).resolve().parent / "bundled_config"
BUNDLED_CONFIG_DIR = (
    _PACKAGED_CONFIG if _PACKAGED_CONFIG.is_dir()
    else Path(__file__).resolve().parents[3] / "config"
)


def resolve_config_file(configured: str | Path, filename: str) -> Path:
    """Use a bundled file only when the configured file is missing.

    Missing bundled files are errors rather than an empty configuration.
    Existing files are returned unchanged so parsing errors remain visible.
    """
    target = Path(configured)
    if target.is_file():
        return target
    fallback = BUNDLED_CONFIG_DIR / filename
    if not fallback.is_file():
        raise FileNotFoundError(f"configuration missing: {target}; bundled copy missing: {fallback}")
    logger.warning("configuration missing: %s; using bundled copy: %s", target, fallback)
    return fallback
