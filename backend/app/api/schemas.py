"""Pydantic response schemas for the landing API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class SourceInfo(BaseModel):
    """Tacview source information for multi-source support (Issue #13)."""

    id: str
    name: str
    connected: bool


class LandingSummary(BaseModel):
    """One row of the landing history list (FR-5 dashboard)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    flight_id: int
    kind: str | None = None
    outcome: str | None = None
    #: "provisional" while the outcome (bolter / touch-and-go dwell) is still
    #: under observation, "final" once confirmed (Issue #5).
    outcome_status: str = "final"
    venue_name: str | None = None
    pilot: str | None = None
    airframe: str | None = None
    #: Mission-relative touchdown time (ACMI seconds since mission start).
    touchdown_time: float | None = None
    #: Wall-clock epoch of the touchdown (Issue D-1): ReferenceTime +
    #: touchdown_time. ``None`` when the ACMI header lacked ReferenceTime.
    touchdown_epoch: float | None = None
    grade: str | None = None
    score: float | None = None
    created_at: datetime | None = None
    #: Source identifier (Issue #13 multi-source support)
    source_id: str | None = None
    #: Source display name (Issue #13 multi-source support)
    source_name: str | None = None
    #: Approach pattern classification: "overhead" | "straight_in" | "unknown"
    approach_pattern: str | None = None


class FactorOut(BaseModel):
    name: str
    #: LSO ファクター (空母) の重大度。陸上の採点コンポーネントには無い。
    severity: str | None = None
    #: 陸上の採点コンポーネントの素点 (0..100) と合成重み。これを返さないと
    #: UI 側に「どの項目で何点落としたか」を出す手段が無く、講評文だけを
    #: 見て理由を推測することになる。LSO ファクター側では None。
    score: float | None = None
    weight: float | None = None
    evidence: dict[str, Any] | None = None


class DeviationSampleOut(BaseModel):
    time: float
    distance_to_go: float
    #: Unclamped along-course position (negative once past the reference).
    #: Needed to plot the break / upwind leg, which ``distance_to_go``
    #: flattens onto the threshold line. ``None`` on older tracks.
    signed_distance_to_go: float | None = None
    glideslope_deviation: float | None = None
    centerline_deviation: float | None = None
    speed: float | None = None
    aoa: float | None = None
    agl: float | None = None
    #: Metres still to fly to the runway threshold (negative once over it);
    #: None when the landing was not resolved to a real runway.
    distance_to_threshold: float | None = None


class ApproachTrackOut(BaseModel):
    kind: str | None = None
    outcome: str | None = None
    glideslope_deg: float | None = None
    course_deg: float | None = None
    touchdown_time: float | None = None
    #: Reference frame the deviations were computed in: carrier FLOLS
    #: geometry, resolved runway geometry, or None for the touchdown-derived
    #: estimate. Without this the UI cannot say which runway was graded.
    geometry: dict[str, Any] | None = None
    samples: list[DeviationSampleOut] = []


class TouchdownState(BaseModel):
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    heading: float | None = None
    speed_ms: float | None = None
    descent_rate_ms: float | None = None


class RegradeRequest(BaseModel):
    """Optional threshold overrides applied on top of the YAML config."""

    overrides: dict[str, Any] | None = None


class RegradeResponse(BaseModel):
    id: int
    grade: str | None = None
    score: float | None = None
    comment: str | None = None
    factors: list[FactorOut] = []
    metrics: dict[str, Any] | None = None


class LandingDetail(LandingSummary):
    """Full evaluation + approach track for one landing."""

    carrier_object_id: int | None = None
    comment: str | None = None
    factors: list[FactorOut] = []
    metrics: dict[str, Any] | None = None
    grading_version: str | None = None
    graded_at: datetime | None = None
    touchdown: TouchdownState | None = None
    approach_track: ApproachTrackOut | None = None


class LandingListResponse(BaseModel):
    items: list[LandingSummary]
    total: int
    limit: int
    offset: int
    #: Available Tacview sources for filtering (Issue #13 multi-source support)
    sources: list[SourceInfo] | None = None


# ---------------------------------------------------------------------------
# ACMI file import (background jobs)
# ---------------------------------------------------------------------------


class ImportJobOut(BaseModel):
    """State / result summary of one ACMI import job."""

    id: str
    filename: str
    #: "pending" | "processing" | "completed" | "failed"
    status: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    frames_processed: int = 0
    total_frames: int = 0
    progress_percent: int | None = None
    landings_detected: int = 0
    duplicates_skipped: int = 0
    error: str | None = None


class ImportStartResponse(BaseModel):
    """Acknowledgement returned immediately by POST /api/import."""

    id: str
    filename: str
    status: str


class ImportJobListResponse(BaseModel):
    items: list[ImportJobOut]
