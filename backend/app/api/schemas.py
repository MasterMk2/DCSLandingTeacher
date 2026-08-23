"""Pydantic response schemas for the landing API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class LandingSummary(BaseModel):
    """One row of the landing history list (FR-5 dashboard)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    flight_id: int
    kind: str | None = None
    outcome: str | None = None
    venue_name: str | None = None
    pilot: str | None = None
    airframe: str | None = None
    touchdown_time: float | None = None
    grade: str | None = None
    score: float | None = None
    created_at: datetime | None = None


class FactorOut(BaseModel):
    name: str
    severity: str | None = None
    evidence: dict[str, Any] | None = None


class DeviationSampleOut(BaseModel):
    time: float
    distance_to_go: float
    glideslope_deviation: float | None = None
    centerline_deviation: float | None = None
    speed: float | None = None
    aoa: float | None = None
    agl: float | None = None


class ApproachTrackOut(BaseModel):
    kind: str | None = None
    outcome: str | None = None
    glideslope_deg: float | None = None
    course_deg: float | None = None
    touchdown_time: float | None = None
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
