"""Integration tests for pipeline configuration reload."""

from pathlib import Path

from app.grading.config import load_grading_config
from app.grading.land_grader import MS_TO_FPM, grade_land_landing
from app.pipeline import LandingPipeline
from tests.conftest import GRADING_YAML
from tests.helpers import analysis_with_gs_deviations


def test_pipeline_reload_config_picks_up_file_changes(tmp_path) -> None:
    config_path = tmp_path / "grading.yaml"
    original = Path(GRADING_YAML).read_text(encoding="utf-8")
    config_path.write_text(original, encoding="utf-8")
    pipeline = LandingPipeline(
        None, load_grading_config(config_path), grading_config_path=config_path
    )
    analysis = analysis_with_gs_deviations([0.0] * 12)
    analysis.touchdown_descent_rate_ms = 350.0 / MS_TO_FPM
    before = next(
        c
        for c in grade_land_landing(analysis, pipeline._config).components
        if c.name == "descent_rate"
    ).score
    modified = original.replace("fair: 450", "fair: 300")
    assert modified != original
    config_path.write_text(modified, encoding="utf-8")
    pipeline.reload_config()
    after = next(
        c
        for c in grade_land_landing(analysis, pipeline._config).components
        if c.name == "descent_rate"
    ).score
    assert after < before
