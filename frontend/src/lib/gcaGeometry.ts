/**
 * Pure coordinate-transform logic for the GCA (PAR) scope views and the
 * top-down track view. All functions are unit-testable without DOM.
 *
 * Backend units (see backend/app/grading/deviations.py):
 * - distance_to_go       : meters ahead of the touchdown point
 * - glideslope_deviation : meters above (+) / below (-) ideal slope
 * - centerline_deviation : meters right (+) of course
 */

import type { DeviationSample } from "../types/api";

/** Normalized point in [0,1] x [0,1] scope space. */
export interface ScopePoint {
  x: number; // 0 = left edge, 1 = right edge
  y: number; // 0 = far end (top), 1 = touchdown (bottom)
}

/** Round up to a "nice" value (1/2/5 x 10^n) for axis scaling. */
export function niceCeil(value: number): number {
  if (value <= 0) return 1;
  const exp = Math.floor(Math.log10(value));
  const base = Math.pow(10, exp);
  const frac = value / base;
  let niceFrac: number;
  if (frac <= 1) niceFrac = 1;
  else if (frac <= 2) niceFrac = 2;
  else if (frac <= 5) niceFrac = 5;
  else niceFrac = 10;
  return niceFrac * base;
}

/**
 * Max absolute deviation used for the horizontal scale, with a floor so a
 * perfectly centered approach does not collapse the scale.
 */
export function computeMaxDeviation(
  samples: DeviationSample[],
  pick: (s: DeviationSample) => number | null | undefined,
  floorMeters = 30,
): number {
  let maxAbs = 0;
  for (const s of samples) {
    const v = pick(s);
    if (v !== null && v !== undefined && Number.isFinite(v)) {
      maxAbs = Math.max(maxAbs, Math.abs(v));
    }
  }
  // Apply the floor after rounding so the axis label stays a round number
  // while never dropping below the requested minimum.
  return Math.max(niceCeil(maxAbs), floorMeters);
}

/** Max distance-to-go rounded to a nice value, floored at 500 m. */
export function computeMaxRange(samples: DeviationSample[], floorMeters = 500): number {
  let maxRange = 0;
  for (const s of samples) {
    if (Number.isFinite(s.distance_to_go)) {
      maxRange = Math.max(maxRange, s.distance_to_go);
    }
  }
  return niceCeil(Math.max(maxRange, floorMeters));
}

/**
 * Horizontal position of a deviation value in [0,1]; 0.5 is dead center
 * (ideal course). Values beyond +/-maxDev clamp to the edges.
 */
export function scopeScaleX(deviation: number, maxDev: number): number {
  if (maxDev <= 0) return 0.5;
  const ratio = deviation / maxDev;
  return Math.min(1, Math.max(0, 0.5 + ratio / 2));
}

/**
 * Vertical position of a distance-to-go in [0,1]; far range is at the top
 * (y=0), touchdown at the bottom (y=1).
 */
export function scopeScaleY(distanceToGo: number, maxRange: number): number {
  if (maxRange <= 0) return 1;
  const clamped = Math.min(Math.max(distanceToGo, 0), maxRange);
  return 1 - clamped / maxRange;
}

/** Azimuth-scope track points (centerline deviation vs range). */
export function azimuthTrackPoints(
  samples: DeviationSample[],
  maxDev: number,
  maxRange: number,
): ScopePoint[] {
  return samples
    .filter((s) => s.centerline_deviation !== null && s.centerline_deviation !== undefined)
    .map((s) => ({
      x: scopeScaleX(s.centerline_deviation as number, maxDev),
      y: scopeScaleY(s.distance_to_go, maxRange),
    }));
}

/** Elevation-scope track points (glideslope deviation vs range). */
export function elevationTrackPoints(
  samples: DeviationSample[],
  maxDev: number,
  maxRange: number,
): ScopePoint[] {
  return samples
    .filter((s) => s.glideslope_deviation !== null && s.glideslope_deviation !== undefined)
    .map((s) => ({
      x: scopeScaleX(s.glideslope_deviation as number, maxDev),
      y: scopeScaleY(s.distance_to_go, maxRange),
    }));
}

/**
 * Distance-ring radii (in meters) between ringStep and maxRange exclusive,
 * e.g. rings(3700, 1000) -> [1000, 2000, 3000].
 */
export function distanceRings(maxRange: number, ringStep: number): number[] {
  const rings: number[] = [];
  for (let d = ringStep; d < maxRange; d += ringStep) rings.push(d);
  return rings;
}

/**
 * Top-down plan-view points in course frame:
 * x = lateral offset normalized by maxLateral (0.5 = centerline),
 * y = distance-to-go normalized by maxRange (touchdown at bottom).
 */
export function topDownPoints(
  samples: DeviationSample[],
  maxLateral: number,
  maxRange: number,
): ScopePoint[] {
  return samples
    .filter((s) => s.centerline_deviation !== null && s.centerline_deviation !== undefined)
    .map((s) => ({
      x: scopeScaleX(s.centerline_deviation as number, maxLateral),
      y: scopeScaleY(s.distance_to_go, maxRange),
    }));
}

/**
 * Descent-rate series derived from consecutive AGL samples (m/s, positive =
 * descending). Returns entries aligned with samples[i >= 1].
 */
export function descentRateSeries(samples: DeviationSample[]): { time: number; rateMs: number }[] {
  const out: { time: number; rateMs: number }[] = [];
  for (let i = 1; i < samples.length; i++) {
    const prev = samples[i - 1];
    const cur = samples[i];
    if (prev.agl === null || prev.agl === undefined) continue;
    if (cur.agl === null || cur.agl === undefined) continue;
    const dt = cur.time - prev.time;
    if (dt <= 0) continue;
    out.push({ time: cur.time, rateMs: (prev.agl - cur.agl) / dt });
  }
  return out;
}
