/**
 * Plan view of the whole circuit: break -> crosswind -> downwind -> base
 * -> final, at equal scale on both axes.
 *
 * The GCA scopes next to it are the right tool for the last mile and the
 * wrong one for the pattern: they scale range and deviation independently,
 * so the turn shape is distorted, and they read `distance_to_go`, which is
 * clamped at zero and folds the whole upwind side onto the threshold line.
 */

import { useMemo } from "react";
import {
  downwindGuide,
  legRuns,
  patternProjection,
  scaleBarLabel,
  type Leg,
  type LegTimes,
} from "../lib/patternGeometry";
import type { ApproachTrack } from "../types/api";

const WIDTH = 460;
const HEIGHT = 460;
const PAD = 30;

const LEG_LABELS: Record<Leg, string> = {
  entry: "イニシャル",
  break: "ブレイク",
  downwind: "ダウンウィンド",
  base: "ベースターン",
  final: "ファイナル",
  rollout: "接地後",
};

export interface PatternTrackProps {
  track: ApproachTrack;
  /** Grading metrics; the pattern leg boundaries live here as mission times. */
  metrics?: Record<string, unknown> | null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function PatternTrack({ track, metrics }: PatternTrackProps) {
  const legTimes: LegTimes = useMemo(
    () => ({
      rollout: num(metrics?.["pattern_rollout_time"]),
      breakStart: num(metrics?.["pattern_break_start_time"]),
      breakEnd: num(metrics?.["pattern_break_end_time"]),
      downwindStart: num(metrics?.["pattern_downwind_start_time"]),
      downwindEnd: num(metrics?.["pattern_downwind_end_time"]),
      touchdown: num(track.touchdown_time),
    }),
    [metrics, track.touchdown_time],
  );

  const projection = useMemo(
    () => patternProjection(track.samples, legTimes, WIDTH, HEIGHT, PAD),
    [track.samples, legTimes],
  );

  if (!projection) {
    return <p className="empty-message">パターンを描けるサンプルがありません。</p>;
  }

  const { toPx, points, scaleBarM, metersPerPx } = projection;
  const runs = legRuns(points);
  const legsShown = Array.from(new Set(runs.map((r) => r.leg)));
  const guide = downwindGuide(
    points,
    num(metrics?.["pattern_downwind_course_offset_deg"]),
    toPx,
  );

  // Runway: known length when the real runway was resolved, otherwise just
  // the extended centerline through the touchdown point.
  const geometry = track.geometry ?? {};
  const runwayLength = num(geometry["length_m"]);
  const aimingPoint = num(geometry["aiming_point_m"]) ?? 0;

  const centerTop = toPx(-4000, 0);
  const centerBottom = toPx(20000, 0);
  const touchdown = toPx(0, 0);
  const barPx = scaleBarM / metersPerPx;
  const start = points[0];

  return (
    <figure className="pattern-view" aria-label="パターン軌跡（プランビュー）">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" className="pattern-svg">
        {/* Everything drawn in map coordinates is clipped to the plot. The
            extended centerline runs to 20 km and the runway is drawn from
            its real threshold, so without this they spill past the frame --
            the runway printed as a fat bar sticking out of the top edge. */}
        <defs>
          <clipPath id="pattern-clip">
            <rect x={1} y={1} width={WIDTH - 2} height={HEIGHT - 2} rx={6} />
          </clipPath>
        </defs>
        <rect
          x={1}
          y={1}
          width={WIDTH - 2}
          height={HEIGHT - 2}
          className="scope-bg"
          rx={6}
        />
        <g clipPath="url(#pattern-clip)">

        {/* Extended runway centerline */}
        <line
          x1={centerTop.px}
          y1={centerTop.py}
          x2={centerBottom.px}
          y2={centerBottom.py}
          className="pattern-centerline"
          strokeDasharray="8 7"
        />

        {/* Runway strip, when its real length is known */}
        {runwayLength !== null && (
          <line
            x1={toPx(aimingPoint, 0).px}
            y1={toPx(aimingPoint, 0).py}
            x2={toPx(aimingPoint - runwayLength, 0).px}
            y2={toPx(aimingPoint - runwayLength, 0).py}
            className="pattern-runway"
            strokeLinecap="butt"
          />
        )}

        {/* Downwind heading: the fitted leg against a runway-parallel
            reference through the same mid-point. */}
        {guide && (
          <g className="pattern-downwind-guide">
            <line
              x1={guide.ideal.x1}
              y1={guide.ideal.y1}
              x2={guide.ideal.x2}
              y2={guide.ideal.y2}
              className="pattern-downwind-ideal"
              strokeDasharray="7 6"
            />
            <line
              x1={guide.actual.x1}
              y1={guide.actual.y1}
              x2={guide.actual.x2}
              y2={guide.actual.y2}
              className="pattern-downwind-fit"
            />
            <text
              x={guide.labelX}
              y={guide.labelY - 8}
              className="pattern-downwind-label"
              textAnchor="middle"
            >
              ダウンウィンド方位差 {Math.abs(guide.offsetDeg).toFixed(1)}°
            </text>
          </g>
        )}

        {runs.map((run, i) => (
          <polyline
            key={`${run.leg}-${i}`}
            className={`pattern-leg pattern-leg-${run.leg}`}
            points={run.points.map((p) => `${p.px},${p.py}`).join(" ")}
          />
        ))}

        {/* Where the recording starts, and the touchdown point */}
        <circle cx={start.px} cy={start.py} r={4} className="pattern-start" />
        <text x={start.px + 7} y={start.py + 4} className="scope-label">
          記録開始
        </text>
        <circle cx={touchdown.px} cy={touchdown.py} r={5} className="scope-touchdown" />
        </g>

        {/* Scale bar + orientation hint */}
        <g transform={`translate(${PAD}, ${HEIGHT - 14})`}>
          <line x1={0} y1={0} x2={barPx} y2={0} className="pattern-scalebar" />
          <line x1={0} y1={-4} x2={0} y2={4} className="pattern-scalebar" />
          <line x1={barPx} y1={-4} x2={barPx} y2={4} className="pattern-scalebar" />
          <text x={barPx + 6} y={4} className="scope-label">
            {scaleBarLabel(scaleBarM)}
          </text>
        </g>
        <text x={WIDTH - PAD} y={PAD} className="scope-label" textAnchor="end">
          ↑ 着陸方向
        </text>
      </svg>
      <figcaption className="pattern-legend">
        {legsShown.map((leg) => (
          <span key={leg} className="pattern-legend-item">
            <span className={`pattern-legend-swatch pattern-leg-${leg}`} />
            {LEG_LABELS[leg]}
          </span>
        ))}
      </figcaption>
    </figure>
  );
}
