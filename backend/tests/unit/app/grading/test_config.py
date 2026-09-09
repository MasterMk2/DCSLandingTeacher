"""Unit tests for app.grading.config."""

from __future__ import annotations

import pytest

from app.grading.config import load_grading_config
from tests.conftest import GRADING_YAML

CONFIG = load_grading_config(GRADING_YAML)


def test_land_glideslope_defaults_to_three_degrees() -> None:
    """Land approaches are graded against a 3-degree path (Issue D-5).

    The default must be 3.0 both in the shipped YAML and in the in-code
    fallback, and ``glideslope_for`` must route land events to it.
    """
    from app.grading.config import GradingConfig

    # Shipped configuration.
    assert CONFIG.land_glideslope_deg == pytest.approx(3.0)
    assert CONFIG.glideslope_for("land") == pytest.approx(3.0)
    # Carrier path stays on the FLOLS 3.5-degree datum.
    assert CONFIG.glideslope_for("carrier") == pytest.approx(3.5)

    # In-code fallback when no YAML is present.
    assert GradingConfig({}).land_glideslope_deg == pytest.approx(3.0)


def test_code_defaults_agree_with_the_shipped_yaml() -> None:
    """``_DEFAULTS`` and ``config/grading.yaml`` must carry the same numbers.

    This is not tidiness. ``load_grading_config()`` falls back to ``_DEFAULTS``
    without a word when the configured path is missing, and in production it
    IS missing -- the container mounts an empty directory over /app/config, so
    the defaults in code are the live configuration and the YAML is inert.
    Any value that lives only in the YAML is therefore silently absent from
    the running server.

    That is how ``lso_grading.factors`` shipped as ``{}``: every carrier
    landing came out "OK" with the comment "On centerline, on glidepath, on
    speed." because not one factor could fire. No test noticed, because every
    LSO test loads the YAML (``CONFIG`` above) and none exercised the path
    production actually runs.

    Compares thresholds only. Prose (``details``) is deliberately kept in the
    YAML alone, so it is excluded rather than duplicated.

    LSO factors that the YAML only DECLARES -- the ones marked
    ``enabled: false`` with no threshold, so the UI could list them -- are
    allowed to be absent from the defaults, because no grade can depend on
    them. That exemption is not taken on trust: it is proved per factor
    below, so adding a real threshold to one of them fails this test.
    """
    import yaml

    from app.grading.config import _DEFAULTS

    with open(GRADING_YAML, encoding="utf-8") as stream:
        shipped = yaml.safe_load(stream)

    PROSE = {"details"}
    #: Keys the LSO detectors actually read off a factor.
    ACTIONABLE = {
        "gs_deviation_m",
        "speed_ratio",
        "lateral_deviation_m",
        "speed_range_ms",
        "auto",
        "extra_descent_ms",
    }

    yaml_factors = shipped["lso_grading"]["factors"]
    declaration_only = set()
    for name, cfg in yaml_factors.items():
        if name in _DEFAULTS["lso_grading"]["factors"]:
            continue
        actionable = ACTIONABLE & set(cfg)
        assert not actionable and cfg.get("enabled") is False, (
            f"lso_grading.factors.{name} is absent from _DEFAULTS but could still "
            f"change a grade (enabled={cfg.get('enabled')!r}, keys={sorted(actionable)}). "
            "Production reads the defaults, so copy it across."
        )
        declaration_only.add(name)
    for name in declaration_only:
        yaml_factors.pop(name)

    def compare(defaults, yaml_side, path: str, mismatches: list[str]) -> None:
        if isinstance(defaults, dict) and isinstance(yaml_side, dict):
            for key in sorted(set(defaults) | set(yaml_side)):
                if key in PROSE:
                    continue
                here = f"{path}.{key}" if path else key
                if key not in defaults:
                    mismatches.append(
                        f"{here}: missing from _DEFAULTS (yaml has {yaml_side[key]!r})"
                    )
                elif key not in yaml_side:
                    mismatches.append(
                        f"{here}: missing from grading.yaml (defaults have {defaults[key]!r})"
                    )
                else:
                    compare(defaults[key], yaml_side[key], here, mismatches)
        elif defaults != yaml_side:
            mismatches.append(f"{path}: defaults={defaults!r} yaml={yaml_side!r}")

    mismatches: list[str] = []
    for section in ("geometry", "approach", "detection", "land_grading", "lso_grading"):
        compare(_DEFAULTS[section], shipped[section], section, mismatches)
    assert not mismatches, "code defaults and grading.yaml disagree:\n  " + "\n  ".join(mismatches)
