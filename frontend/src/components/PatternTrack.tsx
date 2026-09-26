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
  inShipFrame,
  legRuns,
  patternProjection,
  scaleBarLabel,
  type Leg,
  type LegTimes,
  type PatternPoint,
} from "../lib/patternGeometry";
import type { ApproachTrack } from "../types/api";

const WIDTH = 460;
const HEIGHT = 460;
const PAD = 30;

const LEG_LABELS: Record<Leg, string> = {
  prior: "前のパス",
  entry: "イニシャル",
  break: "ブレイク",
  downwind: "ダウンウィンド",
  base: "ベースターン",
  final: "ファイナル",
  rollout: "接地後",
};

/** A Case I names its legs differently: the "base" is the 180 through the
 *  90 and the 45, and the final is the groove. */
const CARRIER_LEG_LABELS: Record<Leg, string> = {
  prior: "ウェーブオフ前のパス",
  entry: "イニシャル",
  break: "ブレイク（キスオフ）",
  downwind: "ダウンウィンド",
  base: "180°ターン",
  final: "グルーブ",
  rollout: "着艦後",
};

/** After a bolter, a touch-and-go or a wave-off the circuit starts with a
 *  climb off the angled deck and a turn onto the downwind: no initial, and
 *  that turn is not a kiss-off (`pattern_entry` = "turn"). */
const CARRIER_TURN_ENTRY_LABELS: Partial<Record<Leg, string>> = {
  entry: "上昇・進入",
  break: "ダウンウィンドへの旋回",
};

/** Case I checkpoints the backend timed (mission seconds), in flying order. */
const CARRIER_MARKS: [string, string][] = [
  ["pattern_low_pass_time", "ウェーブオフ"],
  ["pattern_break_start_time", "キスオフ"],
  ["pattern_abeam_time", "アビーム"],
  ["pattern_ninety_time", "90"],
  ["pattern_wake_time", "ウェイク"],
  ["pattern_groove_start_time", "グルーブ"],
];

/** The plotted point closest in time, if one is within a second of it. */
function pointAt(points: PatternPoint[], time: number | null): PatternPoint | null {
  if (time === null) return null;
  let best: PatternPoint | null = null;
  for (const point of points) {
    if (!best || Math.abs(point.time - time) < Math.abs(best.time - time)) best = point;
  }
  return best && Math.abs(best.time - time) <= 1.0 ? best : null;
}

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
      priorEnd: num(metrics?.["pattern_low_pass_time"]),
      rollout: num(metrics?.["pattern_rollout_time"]),
      breakStart: num(metrics?.["pattern_break_start_time"]),
      breakEnd: num(metrics?.["pattern_break_end_time"]),
      downwindStart: num(metrics?.["pattern_downwind_start_time"]),
      downwindEnd: num(metrics?.["pattern_downwind_end_time"]),
      touchdown: num(track.touchdown_time),
    }),
    [metrics, track.touchdown_time],
  );

  // Carrier tracks referenced to the moving deck come with the ship's own
  // frame: draw those up the ship's heading, the way a Case I is flown.
  const samples = useMemo(() => inShipFrame(track.samples), [track.samples]);
  const shipFrame = samples !== track.samples;

  const projection = useMemo(
    () => patternProjection(samples, legTimes, WIDTH, HEIGHT, PAD),
    [samples, legTimes],
  );

  if (!projection) {
    return <p className="empty-message">パターンを描けるサンプルがありません。</p>;
  }

  const { toPx, points, scaleBarM, metersPerPx } = projection;
  const runs = legRuns(points);
  const legsShown = Array.from(new Set(runs.map((r) => r.leg)));
  const turnEntry = metrics?.["pattern_entry"] === "turn";
  const legLabels = !shipFrame
    ? LEG_LABELS
    : turnEntry
      ? { ...CARRIER_LEG_LABELS, ...CARRIER_TURN_ENTRY_LABELS }
      : CARRIER_LEG_LABELS;
  const marks = shipFrame
    ? CARRIER_MARKS.map(([key, label]) => ({
        label: key === "pattern_break_start_time" && turnEntry ? "旋回開始" : label,
        point: pointAt(points, num(metrics?.[key])),
      })).filter((m): m is { label: string; point: PatternPoint } => m.point !== null)
    : [];
  const fitted = downwindGuide(
    points,
    num(metrics?.["pattern_downwind_course_offset_deg"]),
    toPx,
  );
  // On a Case I the abeam mark sits somewhere along the downwind, which is
  // where the guide puts its label (the leg's middle). Of a few spots along
  // the fitted line, put the label at the one farthest from every mark.
  const guide =
    shipFrame && fitted && marks.length > 0
      ? (() => {
          const { x1, y1, x2, y2 } = fitted.actual;
          const spots = [0.15, 0.3, 0.5, 0.7, 0.85].map((f) => ({
            x: x1 + (x2 - x1) * f,
            y: y1 + (y2 - y1) * f,
          }));
          const clearance = (spot: { x: number; y: number }) =>
            Math.min(...marks.map((m) => Math.hypot(m.point.px - spot.x, m.point.py - spot.y)));
          const best = spots.reduce((a, b) => (clearance(b) > clearance(a) ? b : a));
          return { ...fitted, labelX: best.x, labelY: best.y };
        })()
      : fitted;

  // Runway: known length when the real runway was resolved, otherwise just
  // the extended centerline through the touchdown point.
  const geometry = track.geometry ?? {};
  const runwayLength = num(geometry["length_m"]);
  const aimingPoint = num(geometry["aiming_point_m"]) ?? 0;

  // The ship: its axis (BRC) through the reference point, the hull as far as
  // the stern is known either side of it, and the angled deck from the ramp.
  // Plan coordinates put astern DOWN the page (along = -x).
  const rampX = num(geometry["ramp_along_m"]);
  const rampY = num(geometry["ramp_lateral_m"]);
  const deckAngle = num(geometry["landing_course_offset_deg"]);
  const deckLength = num(geometry["landing_area_length_m"]) ?? 0;
  const deck =
    shipFrame && rampX !== null && rampY !== null && deckAngle !== null
      ? (() => {
          const rad = (deckAngle * Math.PI) / 180;
          const at = (metres: number) =>
            toPx(-(rampX + metres * Math.cos(rad)), rampY + metres * Math.sin(rad));
          return { ramp: at(0), bow: at(deckLength), wake: at(-3000) };
        })()
      : null;

  const centerTop = toPx(-4000, 0);
  const centerBottom = toPx(20000, 0);
  const touchdownPoint = shipFrame
    ? pointAt(points, num(track.touchdown_time))
    : null;
  const touchdown = touchdownPoint
    ? { px: touchdownPoint.px, py: touchdownPoint.py }
    : toPx(0, 0);
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

        {/* The ship and its angled deck (carrier, ship frame). */}
        {shipFrame && rampX !== null && (
          <line
            x1={toPx(-rampX, 0).px}
            y1={toPx(-rampX, 0).py}
            x2={toPx(rampX, 0).px}
            y2={toPx(rampX, 0).py}
            className="pattern-ship"
            strokeLinecap="round"
          />
        )}
        {deck && (
          <>
            <line
              x1={deck.wake.px}
              y1={deck.wake.py}
              x2={deck.ramp.px}
              y2={deck.ramp.py}
              className="pattern-centerline"
              strokeDasharray="3 5"
            />
            <line
              x1={deck.ramp.px}
              y1={deck.ramp.py}
              x2={deck.bow.px}
              y2={deck.bow.py}
              className="pattern-runway"
              strokeLinecap="butt"
            />
          </>
        )}

        {/* Runway strip, when its real length is known */}
        {!shipFrame && runwayLength !== null && (
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

        {/* Case I checkpoints the backend timed: the kiss-off, abeam, the
            90, the wake, the start of the groove. */}
        {marks.map(({ label, point }) => (
          <g key={label} className="pattern-mark">
            <circle cx={point.px} cy={point.py} r={3.5} className="pattern-mark-dot" />
            <text x={point.px + 6} y={point.py - 5} className="scope-label">
              {label}
            </text>
          </g>
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
          {shipFrame ? "↑ 艦首方向（BRC）・艦と一緒に動く座標" : "↑ 着陸方向"}
        </text>
      </svg>
      <figcaption className="pattern-legend">
        {legsShown.map((leg) => (
          <span key={leg} className="pattern-legend-item">
            <span className={`pattern-legend-swatch pattern-leg-${leg}`} />
            {legLabels[leg]}
          </span>
        ))}
      </figcaption>
    </figure>
  );
}
