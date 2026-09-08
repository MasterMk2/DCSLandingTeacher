"""Locating the tuning YAMLs when the configured path is not there.

The problem this solves, measured on the production server 2026-09-05: the
container bind-mounts an EMPTY host directory over ``/app/config``, which
shadows the ``grading.yaml`` baked into the image. ``load_grading_config()``
then hits its "file missing -> built-in defaults" branch and returns silently,
so the server ran for weeks on the code defaults while the repository's
carefully commented YAML was inert. Nothing in the logs said so. The visible
symptom was that every carrier landing graded "OK", because the LSO factor
table was then missing from the code defaults and existed only in the YAML.
That particular hole is closed from the other side too -- the defaults now
carry the same thresholds, and a test pins them equal -- but the class of
failure is not: any value that lives only in the YAML is absent from a
server whose configured path is empty, and it is absent silently.

Two independent guards, because they fail differently:

- **Say so.** :func:`resolve_config_path` logs a WARNING naming the path that
  was configured and missing. This alone would have caught it. It costs one
  log line and cannot break anything.
- **Have a copy the mount cannot reach.** A bind mount can only shadow the
  path it is mounted on. The installed package lives in site-packages, so a
  copy of the YAML placed *inside the package* is always readable however
  ``/app/config`` is mounted. The image build puts it there (see
  ``docker/backend.Dockerfile``); in a source checkout it is simply absent
  and this module reports nothing to fall back to, which is correct -- there
  the repository's ``config/`` is the real file and is found normally.

There is deliberately only ONE copy of each YAML in git. The packaged copy is
produced at build time from ``config/*.yaml``, so the two cannot drift.
"""

from __future__ import annotations

import logging

from importlib import resources
from pathlib import Path

logger = logging.getLogger(__name__)

#: Package, and the subdirectory in it, that the image build copies
#: ``config/*.yaml`` into. ``defaults`` is data inside ``app.grading``, not a
#: package of its own, so it is reached with joinpath rather than named as a
#: module -- there is no ``__init__.py`` in it and there should not be.
PACKAGED_ANCHOR = "app.grading"
PACKAGED_SUBDIR = "defaults"


def packaged_config(filename: str) -> Path | None:
    """Path to the copy shipped inside the package, or ``None`` in a checkout."""
    try:
        candidate = resources.files(PACKAGED_ANCHOR).joinpath(PACKAGED_SUBDIR, filename)
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    try:
        if not candidate.is_file():
            return None
    except OSError:  # pragma: no cover - unusual loaders
        return None
    # Only real files are useful here: both loaders open by path, and the
    # packaged copy is a plain file in every packaging mode this ships in.
    return Path(str(candidate))


def resolve_config_path(configured: str | Path | None, filename: str) -> Path | None:
    """Where to actually read ``filename`` from, warning when it is not where asked.

    Returns the configured path when it exists, else the packaged copy, else
    ``None`` (the caller's own built-in defaults, as before).
    """
    if configured is not None:
        target = Path(configured)
        if target.is_file():
            return target
        fallback = packaged_config(filename)
        logger.warning(
            "configured config %s not found; %s",
            target,
            f"using the copy shipped inside the package ({fallback})"
            if fallback is not None
            # Not necessarily the code defaults: returning None lets the
            # loader try its own CWD-relative default path first, and only
            # then fall back to what is compiled in.
            else "falling back to the loader's default path, then to the built-in defaults in code",
        )
        return fallback
    return packaged_config(filename)
