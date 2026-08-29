/**
 * Plan-view geometry for a whole approach pattern.
 *
 * Distinct from `gcaGeometry`, which draws precision-approach SCOPES: those
 * put range on one axis and deviation on the other and scale each axis
 * independently, because a controller reads them as two separate error
 * needles. A pattern view is a map. The two axes must share one scale or
 * the shape lies -- a 180 deg turn comes out as an ellipse and there is no
 * way to see whether the base turn was tight or wide, which is the whole
 * reason to look at it.
 */

import type { DeviationSample } from "../types/api";

export const M_PER_NM = 1852;

export type Leg =
  | "entry"
  | "break"
  | "downwind"
  | "base"
  | "final"
  | "rollout";

export interface LegTimes {
  rollout?: number | null;
  breakStart?: number | null;
  breakEnd?: number | null;
  downwindStart?: number | null;
  downwindEnd?: number | null;
  touchdown?: number | null;
}

/**
 * Along-course position: metres still to fly, NEGATIVE once past the
 * reference point.
 *
 * `distance_to_go` is clamped at zero, which flattens the break and the
 * upwind leg -- everything beyond the aiming point -- onto the threshold
 * line. Tracks recorded before `signed_distance_to_go` existed only have
 * the clamped value; they still draw correctly for the part of the pattern
 * that is short of the threshold, which is all they captured anyway.
 */
export function alongOf(s: DeviationSample): number | null {
  if (typeof s.signed_distance_to_go === "number") return s.signed_distance_to_go;
  if (typeof s.distance_to_go === "number") return s.distance_to_go;
  return null;
}

/** Which leg a sample belongs to; everything collapses to "final" when the
 *  backend did not report any pattern boundaries (straight-in, old data). */
export function legAt(time: number, t: LegTimes): Leg {
  if (t.touchdown != null && time > t.touchdown) return "rollout";
  if (t.rollout != null && time >= t.rollout) return "final";
  if (t.downwindEnd != null && time > t.downwindEnd) return "base";
  if (t.downwindStart != null && time < t.downwindStart) {
    // The break is the turn off the initial; before it the aircraft is
    // still inbound on the initial. Separating them is what makes "the
    // break was flown level" readable off the picture.
    if (t.breakStart != null && t.breakEnd != null) {
      return time >= t.breakStart && time <= t.breakEnd ? "break" : "entry";
    }
    return "entry";
  }
  if (t.downwindStart != null) return "downwind";
  return "final";
}

export interface PatternPoint {
  px: number;
  py: number;
  /** Along-course metres (negative past the reference) -- kept so overlays
   *  can be computed in the real frame rather than back-projected. */
  alongM: number;
  lateralM: number;
  time: number;
  leg: Leg;
}

export interface PatternProjection {
  /** Metres -> pixels. Same scale on both axes. */
  toPx(alongM: number, lateralM: number): { px: number; py: number };
  metersPerPx: number;
  points: PatternPoint[];
  /** Nice round distance for the scale bar, in metres. */
  scaleBarM: number;
}

const SCALE_BAR_NM = [0.1, 0.25, 0.5, 1, 2, 5, 10];

/**
 * Fit the track into `width` x `height` at equal scale.
 *
 * The touchdown point is forced into the bounds so the runway end is always
 * on screen even when the recording starts and ends off to one side.
 *
 * Returns null when there is nothing plottable.
 */
export function patternProjection(
  samples: DeviationSample[],
  legTimes: LegTimes,
  width: number,
  height: number,
  pad: number,
  minSpanM = 600,
): PatternProjection | null {
  const usable = samples
    .map((s) => ({
      along: alongOf(s),
      lateral: s.centerline_deviation,
      time: s.time,
    }))
    .filter(
      (p): p is { along: number; lateral: number; time: number } =>
        p.along !== null && p.lateral !== null && p.lateral !== undefined,
    );
  if (usable.length < 2) return null;

  let alongMin = 0;
  let alongMax = 0;
  let lateralMin = 0;
  let lateralMax = 0;
  for (const p of usable) {
    alongMin = Math.min(alongMin, p.along);
    alongMax = Math.max(alongMax, p.along);
    lateralMin = Math.min(lateralMin, p.lateral);
    lateralMax = Math.max(lateralMax, p.lateral);
  }
  const alongSpan = Math.max(alongMax - alongMin, minSpanM);
  const lateralSpan = Math.max(lateralMax - lateralMin, minSpanM);
  const alongMid = (alongMax + alongMin) / 2;
  const lateralMid = (lateralMax + lateralMin) / 2;

  const drawW = width - pad * 2;
  const drawH = height - pad * 2;
  // 1.06 leaves a hair of margin so the track never touches the frame.
  const scale = Math.min(drawW / (lateralSpan * 1.06), drawH / (alongSpan * 1.06));
  const centerX = pad + drawW / 2;
  const centerY = pad + drawH / 2;

  // The aircraft flies UP the page: larger distance-to-go is further from
  // the runway, so it sits lower. This is the orientation of every pattern
  // diagram and approach plate -- and it makes "right of course" the right
  // of the page, which a scope view (range vertical, touchdown at bottom)
  // does not.
  const toPx = (alongM: number, lateralM: number) => ({
    px: centerX + (lateralM - lateralMid) * scale,
    py: centerY + (alongM - alongMid) * scale,
  });

  const metersPerPx = 1 / scale;
  const maxBarM = (drawW * metersPerPx) / 3;
  let scaleBarM = SCALE_BAR_NM[0] * M_PER_NM;
  for (const nm of SCALE_BAR_NM) {
    if (nm * M_PER_NM <= maxBarM) scaleBarM = nm * M_PER_NM;
  }

  const points = usable.map((p) => {
    const { px, py } = toPx(p.along, p.lateral);
    return {
      px,
      py,
      alongM: p.along,
      lateralM: p.lateral,
      time: p.time,
      leg: legAt(p.time, legTimes),
    };
  });

  return { toPx, metersPerPx, points, scaleBarM };
}

/** Split into consecutive same-leg runs, repeating the boundary point so
 *  the polylines join without a visible gap. */
export function legRuns(points: PatternPoint[]): { leg: Leg; points: PatternPoint[] }[] {
  const runs: { leg: Leg; points: PatternPoint[] }[] = [];
  for (const point of points) {
    const current = runs[runs.length - 1];
    if (!current || current.leg !== point.leg) {
      const started = current ? [current.points[current.points.length - 1], point] : [point];
      runs.push({ leg: point.leg, points: started });
    } else {
      current.points.push(point);
    }
  }
  return runs;
}

export interface Segment {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface DownwindGuide {
  /** Where the downwind leg WOULD have run if it were parallel to the runway. */
  ideal: Segment;
  /** The straight line fitted to the leg by the backend. */
  actual: Segment;
  labelX: number;
  labelY: number;
  offsetDeg: number;
}

/**
 * The two lines that show whether the downwind was flown parallel to the
 * runway: the fitted leg and a runway-parallel reference through the same
 * mid-point, so the gap between them IS the heading error.
 *
 * The angle comes from the backend (`pattern_downwind_course_offset_deg`),
 * not from a fit repeated here: two fits of the same leg in two languages
 * drift apart, and then the picture and the score disagree about a number
 * they are both naming "方位差".
 */
export function downwindGuide(
  points: PatternPoint[],
  offsetDeg: number | null | undefined,
  toPx: (alongM: number, lateralM: number) => { px: number; py: number },
  extendM = 250,
): DownwindGuide | null {
  if (offsetDeg === null || offsetDeg === undefined || !Number.isFinite(offsetDeg)) {
    return null;
  }
  const leg = points.filter((p) => p.leg === "downwind");
  if (leg.length < 2) return null;

  let alongMin = Infinity;
  let alongMax = -Infinity;
  let alongSum = 0;
  let lateralSum = 0;
  for (const p of leg) {
    alongMin = Math.min(alongMin, p.alongM);
    alongMax = Math.max(alongMax, p.alongM);
    alongSum += p.alongM;
    lateralSum += p.lateralM;
  }
  const centerAlong = alongSum / leg.length;
  const centerLateral = lateralSum / leg.length;
  const lo = alongMin - extendM;
  const hi = alongMax + extendM;
  const slope = Math.tan((offsetDeg * Math.PI) / 180);
  const lateralAt = (alongM: number) => centerLateral + slope * (alongM - centerAlong);

  const idealLo = toPx(lo, centerLateral);
  const idealHi = toPx(hi, centerLateral);
  const actualLo = toPx(lo, lateralAt(lo));
  const actualHi = toPx(hi, lateralAt(hi));
  const mid = toPx(centerAlong, lateralAt(centerAlong));

  return {
    ideal: { x1: idealLo.px, y1: idealLo.py, x2: idealHi.px, y2: idealHi.py },
    actual: { x1: actualLo.px, y1: actualLo.py, x2: actualHi.px, y2: actualHi.py },
    labelX: mid.px,
    labelY: mid.py,
    offsetDeg,
  };
}

/** Scale bar label, e.g. "0.5 nm". */
export function scaleBarLabel(meters: number): string {
  const nm = meters / M_PER_NM;
  return `${nm < 1 ? nm.toFixed(2).replace(/0+$/, "").replace(/\.$/, "") : nm} nm`;
}
