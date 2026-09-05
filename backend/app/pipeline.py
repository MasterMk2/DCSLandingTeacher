"""Landing grading pipeline: detection event -> grade -> DB -> notification.

Connects the ingestor's landing listener to the graders and persistence
(FR-3, FR-4, FR-7). The raw approach segment with computed deviations is
stored alongside the evaluation so thresholds can be re-applied later via
``POST /api/landings/{id}/regrade`` without re-parsing any ACMI data.
"""

from __future__ import annotations

import math

from datetime import datetime, timezone
from logging import getLogger
from pathlib import Path
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
from app.grading.pattern import effective_approach_pattern
from app.ingest import LandingContext
from app.models.entities import DcsObject, Landing

logger = getLogger(__name__)


def _reapply_reference_slope(analysis: ApproachAnalysis, slope_deg: float) -> None:
    """Re-anchor the stored deviations to a (possibly changed) reference slope.

    ``glideslope_deviation`` is metres above/below the ideal path, so it is a
    function of the reference angle. Changing the angle without recomputing
    it would leave the charts drawing an ideal line that the plotted
    deviations no longer refer to.
    """
    if abs(slope_deg - analysis.glideslope_deg) < 1e-9:
        return
    analysis.glideslope_deg = slope_deg
    tan_slope = math.tan(math.radians(slope_deg))
    for sample in analysis.samples:
        if sample.agl is not None:
            sample.glideslope_deviation = sample.agl - sample.distance_to_go * tan_slope

# 2 (2026-09-05): 測れなかった採点項目は中立点 50 ではなく重みごと外す /
# 進入パターンを軌跡から決め直す / ヘリの接地速度比・進入角は採点しない /
# 基準速度をファイナル末尾の保持速度で取る / 記録が足りない着陸には成績を
# 付けない。既存の着陸は再採点しないと v1 の点数のまま残る。
GRADING_VERSION = "2"


def _row_approach_pattern(
    event: LandingEvent, result: LandGradeResult | LsoGradeResult
) -> str | None:
    """``landings`` 行に書く進入パターン。

    陸上では採点側が軌跡から決め直した値が正で、検出器のラベル
    (``event.approach_pattern``) は取り込み時の見込みでしかない。書き戻さ
    ないと、詳細画面が「オーバーヘッド」と表示したまま採点だけが別の
    判断で動く、という食い違いが残る。
    """
    if isinstance(result, LandGradeResult):
        return result.metrics.get("approach_pattern") or event.approach_pattern
    return event.approach_pattern


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
        grading_config_path: Any | None = None,
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
        # Source path so the config can be reloaded at runtime (Issue #40).
        self._config_path = (
            Path(grading_config_path) if grading_config_path is not None else None
        )

    def reload_config(self) -> None:
        """Reload grading thresholds from disk without restarting (Issue #40).

        New landings use the fresh values immediately. A missing path (config
        built in-memory) is a no-op so tests and embedded configs are untouched.
        """
        if self._config_path is None or not self._config_path.is_file():
            return
        from app.grading.config import load_grading_config

        self._config = load_grading_config(self._config_path)
        logger.info("grading config reloaded from %s", self._config_path)

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
            landing.approach_pattern = _row_approach_pattern(event, result)
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
            self._config.glideslope_for(event.kind, event.approach_pattern),
            geometry=self._resolve_geometry(event),
            runway=runway,
            airframe=context.airframe,
        )
        if event.kind == "carrier":
            return analysis, grade_carrier_approach(analysis, self._config), None
        # 進入パターンは軌跡から決め直す。検出器のラベル (取り込み時に
        # ヘディング変化率だけで付けた見込み値) は ``analysis`` にヒントと
        # して残したまま、基準スロープの選択と採点にはこちらを使う。
        # 確定値は result.metrics["approach_pattern"] に載り、行にはそれを
        # 書く (:meth:`_persist` / :meth:`finalize_landing`)。
        _reapply_reference_slope(
            analysis,
            self._config.glideslope_for(
                "land",
                effective_approach_pattern(analysis, self._config.land_grading),
            ),
        )
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
        # This payload is inserted straight into the dashboard's list as a
        # row, so it has to carry every field that row renders. It did not
        # carry ``score`` -- the argument was accepted and dropped -- and the
        # table called ``score.toFixed()`` on the resulting ``undefined``,
        # which throws, unmounts the React tree and leaves a blank page. Any
        # field added to the list view has to be added here too.
        return {
            "id": landing_id,
            "flight_id": context.flight_id,
            "source_id": context.source_id,
            "source_name": context.source_id,
            "kind": event.kind,
            "outcome": event.outcome,
            "outcome_status": status,
            "created_at": _utcnow().isoformat(),
            "grade": result.grade,
            "score": score,
            "approach_pattern": _row_approach_pattern(event, result),
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

    async def _airframe_of(self, landing: Landing) -> str | None:
        """ACMI name of the aircraft that flew this landing, or ``None``."""
        from sqlalchemy import select

        if landing.object_id is None:
            return None
        async with self._session_factory() as session:
            result = await session.execute(
                select(DcsObject.name).where(DcsObject.id == landing.object_id)
            )
            return result.scalar_one_or_none()

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
        try:
            analysis = ApproachAnalysis.from_dict(landing.approach_track)
        except ValueError as exc:
            # Corrupt stored JSON (Issue #44): report it instead of a raw 500.
            from app.api.errors import AppError

            raise AppError(
                422, "MALFORMED_APPROACH_TRACK", str(exc)
            ) from exc
        # 進入パターンと機体は landings / objects 側が正。approach_track に
        # 入れるようにしたのは後からなので、それ以前に記録された着陸では
        # 空のままになる。ここで補わないと、既存データの再採点だけ
        # 「パターン不明・機体不明」で採点されて結果が食い違う。
        #
        # 補うのはあくまで **ヒント** としてのラベル。行の値が過去の再採点で
        # 確定させた値であっても、オーバーヘッドかどうかは毎回軌跡から
        # 決め直すので、古い誤ラベルが再採点のたびに勝ち続けることはない。
        if analysis.approach_pattern in (None, "", "unknown"):
            analysis.approach_pattern = landing.approach_pattern or "unknown"
        if analysis.airframe is None:
            analysis.airframe = await self._airframe_of(landing)
        if landing.kind != "carrier":
            # 基準スロープは設定から引き直す。保存済みの値を使い続けると
            # `overhead_glideslope_deg` を変えても既存の着陸に反映されず、
            # 「設定を変えて再採点」が効かない。
            #
            # パターンは軌跡から決め直したものを使う: 行に入っている値
            # (検出器のラベル) で基準スロープを選ぶと、採点が使うパターン
            # と食い違ったままスロープだけ別物になる。
            _reapply_reference_slope(
                analysis,
                config.glideslope_for(
                    "land",
                    effective_approach_pattern(analysis, config.land_grading),
                ),
            )
        if landing.kind == "carrier":
            result = grade_carrier_approach(analysis, config)
            score = None
        else:
            result = grade_land_landing(analysis, config)
            score = result.score

        async with self._session_factory() as session:
            # The caller may hold the entity in a different session; merge it.
            landing = await session.merge(landing)
            landing.approach_track = analysis.as_dict()
            landing.grade = result.grade
            landing.score = score
            landing.comment = result.comment
            landing.factors = result.factors_payload()
            landing.metrics = dict(result.metrics)
            # 陸上は採点側が軌跡から決め直したパターンを行にも反映する。
            # ここを書かないと、詳細画面のラベルだけ検出器の見込み値のまま
            # 残り、採点は別の判断で動く。
            if landing.kind != "carrier":
                landing.approach_pattern = (
                    result.metrics.get("approach_pattern") or landing.approach_pattern
                )
            landing.grading_version = GRADING_VERSION
            landing.graded_at = _utcnow()
            await session.commit()
            approach_pattern = landing.approach_pattern
        return {
            "id": landing.id,
            "grade": result.grade,
            "score": score,
            "comment": result.comment,
            "factors": result.factors_payload(),
            "metrics": dict(result.metrics),
            "approach_pattern": approach_pattern,
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
                approach_pattern=_row_approach_pattern(event, result),
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
