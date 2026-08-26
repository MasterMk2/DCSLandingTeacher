"""Landing grading pipeline: detection event -> grade -> DB -> notification.

Connects the ingestor's landing listener to the graders and persistence
(FR-3, FR-4, FR-7). The raw approach segment with computed deviations is
stored alongside the evaluation so thresholds can be re-applied later via
``POST /api/landings/{id}/regrade`` without re-parsing any ACMI data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from logging import getLogger
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.detection.detector import LandingEvent
from app.grading.carriers import (
    CarrierGeometryBook,
)
from app.grading.config import GradingConfig
from app.grading.deviations import ApproachAnalysis, build_approach_analysis
from app.grading.land_grader import LandGradeResult, grade_land_landing
from app.grading.lso_grader import LsoGradeResult, grade_carrier_approach
from app.ingest import LandingContext
from app.models.entities import DcsObject, Landing

logger = getLogger(__name__)

GRADING_VERSION = "1"


def _touchdown_epoch(
    reference_time: str | None, mission_time: float | None
) -> float | None:
    """Wall-clock epoch of a touchdown (Issue D-1).

    ACMI times are seconds since mission start; the wall-clock instant is
    ``ReferenceTime`` (ISO-8601 in the global object) plus that offset.
    Returns ``None`` when the header is missing or unparsable so clients can
    fall back to displaying the raw mission time.
    """
    if reference_time is None or mission_time is None:
        return None
    try:
        base = datetime.fromisoformat(reference_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    return base.timestamp() + mission_time


class LandingPipeline:
    """Grade detected landings, persist them, and fan out notifications."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        grading_config: GradingConfig,
        notifier: Any | None = None,
        carrier_geometry_book: CarrierGeometryBook | None = None,
        runway_provider: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = grading_config
        self._notifier = notifier
        # Per-carrier FLOLS geometry (Issue #3); an empty book means every
        # carrier uses the legacy touchdown-referenced approximation.
        self._geometry_book = carrier_geometry_book or CarrierGeometryBook({})
        # Real runway geometry for land landings; ``None`` falls back to the
        # touchdown-referenced approximation.
        self._runway_provider = runway_provider

    async def handle_landing(self, context: LandingContext) -> int | None:
        """Grade + persist one detected landing; returns the row id.

        Two-phase confirmation (Issue #5): a touchdown whose outcome is still
        under observation (bolter / touch-and-go dwell) is persisted as
        ``outcome_status="provisional"`` and broadcast immediately; the final
        verdict arrives later via :meth:`finalize_landing`.
        """
        event = context.event
        analysis, result, score = self._grade(context, await self._resolve_runway(event))
        status = "final" if event.finalized else "provisional"
        landing_id = await self._persist(context, analysis, result, score, status)

        if self._notifier is not None and landing_id is not None:
            try:
                await self._notifier.broadcast_landing(
                    self._payload(landing_id, context, result, score, status),
                    message_type="landing",
                )
            except Exception:
                logger.exception("landing notification failed")
        return landing_id

    async def finalize_landing(self, landing_id: int, context: LandingContext) -> None:
        """Confirm a provisional landing once its outcome is settled (Issue #5).

        Re-grades the (now longer) approach segment, updates the stored row in
        place, and broadcasts a ``landing_update`` message so connected
        clients can replace the provisional entry.
        """
        event = context.event
        analysis, result, score = self._grade(context, await self._resolve_runway(event))
        async with self._session_factory() as session:
            landing = await session.get(Landing, landing_id)
            if landing is None:
                logger.warning(
                    "cannot finalize landing #%d: row disappeared", landing_id
                )
                return
            landing.kind = event.kind
            landing.outcome = event.outcome
            landing.outcome_status = "final"
            # The touchdown itself moves between the provisional and final
            # analysis: bounces absorbed after the first report shift it to
            # the last contact of the sequence. Refresh everything derived
            # from it, or the row keeps describing the first bounce.
            touchdown = event.touchdown
            landing.touchdown_time = touchdown.time
            landing.latitude = touchdown.latitude
            landing.longitude = touchdown.longitude
            landing.altitude = touchdown.altitude
            landing.heading = touchdown.heading
            landing.speed = touchdown.speed
            landing.descent_rate = touchdown.descent_rate_ms
            landing.grade = result.grade
            landing.score = score
            landing.comment = result.comment
            landing.factors = result.factors_payload()
            landing.metrics = dict(result.metrics)
            landing.approach_track = analysis.as_dict()
            landing.approach_pattern = event.approach_pattern
            landing.grading_version = GRADING_VERSION
            landing.graded_at = _utcnow()
            await session.commit()

        if self._notifier is not None:
            try:
                await self._notifier.broadcast_landing(
                    self._payload(landing_id, context, result, score, "final"),
                    message_type="landing_update",
                )
            except Exception:
                logger.exception("landing update notification failed")

    # ------------------------------------------------------------------

    def _resolve_geometry(self, event: LandingEvent):
        """Per-carrier FLOLS geometry for this event (Issue #3)."""
        if event.kind != "carrier":
            return None
        return self._geometry_book.resolve(event.carrier_name, event.carrier_type)

    async def _resolve_runway(self, event: LandingEvent):
        """Real runway geometry for a land landing (``None`` when unknown)."""
        if event.kind != "land" or self._runway_provider is None:
            return None
        try:
            return await self._runway_provider.resolve(
                event.touchdown.latitude,
                event.touchdown.longitude,
                event.touchdown.heading,
            )
        except Exception:
            logger.warning("runway lookup failed", exc_info=True)
            return None

    def _grade(
        self,
        context: LandingContext,
        runway: Any | None = None,
    ) -> tuple[ApproachAnalysis, LandGradeResult | LsoGradeResult, float | None]:
        event = context.event
        analysis = build_approach_analysis(
            event,
            self._config.glideslope_for(event.kind),
            geometry=self._resolve_geometry(event),
            runway=runway,
        )
        if event.kind == "carrier":
            return analysis, grade_carrier_approach(analysis, self._config), None
        result = grade_land_landing(analysis, self._config)
        return analysis, result, result.score

    def _payload(
        self,
        landing_id: int,
        context: LandingContext,
        result: LandGradeResult | LsoGradeResult,
        score: float | None,
        status: str,
    ) -> dict[str, Any]:
        event = context.event
        return {
            "id": landing_id,
            "kind": event.kind,
            "outcome": event.outcome,
            "outcome_status": status,
            "grade": result.grade,
            "pilot": context.pilot,
            "airframe": context.airframe,
            "venue_name": event.carrier_name if event.kind == "carrier" else None,
            # Mission-relative time (ACMI seconds since mission start).
            "touchdown_time": event.touchdown.time,
            # Wall-clock epoch (Issue D-1): ReferenceTime + mission time so
            # clients can display the real-world datetime of the touchdown.
            "touchdown_epoch": _touchdown_epoch(
                context.flight_reference_time, event.touchdown.time
            ),
        }

    async def regrade(
        self, landing: Landing, overrides: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Re-apply current thresholds to a stored approach track (FR-7).

        ``overrides`` is an optional nested dict merged on top of the YAML
        configuration for this single call (e.g. tightened HIGH threshold).
        """
        if not landing.approach_track:
            raise ValueError("landing has no stored approach track")
        config = self._config
        if overrides:
            from app.grading.config import apply_config_overrides

            config = apply_config_overrides(config, overrides)
        analysis = ApproachAnalysis.from_dict(landing.approach_track)
        if landing.kind == "carrier":
            result = grade_carrier_approach(analysis, config)
            score = None
        else:
            result = grade_land_landing(analysis, config)
            score = result.score

        async with self._session_factory() as session:
            # The caller may hold the entity in a different session; merge it.
            landing = await session.merge(landing)
            landing.grade = result.grade
            landing.score = score
            landing.comment = result.comment
            landing.factors = result.factors_payload()
            landing.metrics = dict(result.metrics)
            landing.grading_version = GRADING_VERSION
            landing.graded_at = _utcnow()
            await session.commit()
        return {
            "id": landing.id,
            "grade": result.grade,
            "score": score,
            "comment": result.comment,
            "factors": result.factors_payload(),
            "metrics": dict(result.metrics),
        }

    # ------------------------------------------------------------------

    async def _persist(
        self,
        context: LandingContext,
        analysis: ApproachAnalysis,
        result: LandGradeResult | LsoGradeResult,
        score: float | None,
        outcome_status: str = "final",
    ) -> int | None:
        event: LandingEvent = context.event
        touchdown = event.touchdown
        async with self._session_factory() as session:
            # Row ids come from the ingestor: the ingest transaction may not
            # be committed yet, so re-querying could miss the rows.
            carrier_row_id = context.carrier_row_id
            if carrier_row_id is None:
                carrier_row_id = await self._resolve_object_row(
                    session, context.flight_id, event.carrier_obj_id
                )
            aircraft_row_id = context.object_row_id
            if aircraft_row_id is None:
                aircraft_row_id = await self._resolve_object_row(
                    session, context.flight_id, context.acmi_object_id
                )
            if aircraft_row_id is None or context.flight_id is None:
                logger.warning(
                    "cannot persist landing: missing object/flight rows (obj=%s)",
                    context.acmi_object_id,
                )
                return None

            venue = None
            if event.kind == "carrier":
                venue = event.carrier_name

            landing = Landing(
                flight_id=context.flight_id,
                source_id=context.source_id,
                object_id=aircraft_row_id,
                carrier_object_id=carrier_row_id,
                kind=event.kind,
                outcome=event.outcome,
                outcome_status=outcome_status,
                touchdown_time=touchdown.time,
                venue_name=venue,
                latitude=touchdown.latitude,
                longitude=touchdown.longitude,
                altitude=touchdown.altitude,
                heading=touchdown.heading,
                speed=touchdown.speed,
                descent_rate=touchdown.descent_rate_ms,
                grade=result.grade,
                score=score,
                comment=result.comment,
                factors=result.factors_payload(),
                metrics=dict(result.metrics),
                approach_track=analysis.as_dict(),
                approach_pattern=event.approach_pattern,
                grading_version=GRADING_VERSION,
                graded_at=_utcnow(),
            )
            session.add(landing)
            await session.commit()
            await session.refresh(landing)
            logger.info(
                "landing #%d graded %s (%s)", landing.id, result.grade, event.kind
            )
            return landing.id

    async def _resolve_object_row(
        self, session: AsyncSession, flight_id: int | None, acmi_id: str | None
    ) -> int | None:
        from sqlalchemy import select

        if flight_id is None or not acmi_id:
            return None
        result = await session.execute(
            select(DcsObject.id).where(
                DcsObject.flight_id == flight_id,
                DcsObject.acmi_id == acmi_id,
            )
        )
        return result.scalar_one_or_none()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
