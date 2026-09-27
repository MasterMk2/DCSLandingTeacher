/**
 * Geometry for the 3D flight-path view: where the aircraft was, which way
 * its nose pointed, and where the runway or the deck sits -- all in the
 * frame the plan view already draws in, so the two pictures agree.
 *
 * Scene axes (metres, right-handed, Y up like three.js):
 *   x  forward along the frame's axis (the landing course; for a carrier in
 *      the ship's frame, the ship's heading), 0 at the reference point
 *   y  height above the landing surface (runway / flight deck)
 *   z  right of the axis
 *
 * Kept free of three.js so it can be tested in vitest's node environment;
 * the component only turns these numbers into meshes.
 */

import { alongOf, inShipFrame, legAt, type Leg, type LegTimes } from "./patternGeometry";
import type { ApproachTrack, DeviationSample } from "../types/api";

export type Vec3 = [number, number, number];

const DEG = Math.PI / 180;
const G = 9.80665;

/** Half-width of the window the fallbacks differentiate the track over (s).
 *  Wide enough to average out the ~0.2 s ACMI sampling, narrow enough that
 *  a break turn is not smeared into its neighbours. */
const DERIVATIVE_HALF_WINDOW_S = 1.0;

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Angle folded into (-180, 180]. */
export function wrap180(deg: number): number {
  const r = ((((deg + 180) % 360) + 360) % 360) - 180;
  return r === -180 ? 180 : r;
}

export interface FlightPoint {
  time: number;
  x: number;
  y: number;
  z: number;
  /** Attitude relative to the frame, degrees: heading = nose right of +x,
   *  pitch = nose up, roll = right wing down. */
  heading: number;
  pitch: number;
  roll: number;
  /** Which of the three angles are estimates rather than the recording. */
  estimated: { heading: boolean; pitch: boolean; roll: boolean };
  leg: Leg;
  sample: DeviationSample;
}

export interface FlightPath {
  points: FlightPoint[];
  /** True heading of the +x axis (deg), or null when unknown. */
  axisDeg: number | null;
  /** Carrier track drawn in the ship's own frame (see `inShipFrame`). */
  shipFrame: boolean;
  /** How many points had each angle estimated -- for the caption. */
  estimatedCount: { heading: number; pitch: number; roll: number };
}

/**
 * The frame's axis as a true heading.
 *
 * Land and older carrier tracks: the landing course the samples were
 * projected on. The ship frame: the ship's heading at touchdown, the same
 * axis the backend's `ship_frame_view` uses. A ship that turned during the
 * pass leaves the earlier noses off by that turn -- `ship_heading_change_deg`
 * says how much; Case I is flown against a ship steaming straight.
 */
export function frameAxisDeg(track: ApproachTrack, shipFrame: boolean): number | null {
  const geometry = track.geometry ?? {};
  const course = num(track.course_deg);
  if (!shipFrame) return course;
  const shipHeading = num(geometry["ship_heading_deg"]);
  if (shipHeading !== null) return shipHeading;
  const offset = num(geometry["landing_course_offset_deg"]);
  return course !== null && offset !== null ? course - offset : null;
}

interface Base {
  time: number;
  x: number;
  y: number;
  z: number;
  sample: DeviationSample;
}

/** Indices of the neighbours at most `half` seconds either side of `i`,
 *  widest first -- the chord the fallbacks differentiate over. */
function neighbours(points: Base[], i: number, half: number): [number, number] {
  let lo = i;
  let hi = i;
  while (lo > 0 && points[i].time - points[lo - 1].time <= half) lo -= 1;
  while (hi < points.length - 1 && points[hi + 1].time - points[i].time <= half) hi += 1;
  if (lo === hi) {
    // Sparse data (1 Hz and gaps): fall back to the adjacent samples.
    lo = Math.max(0, i - 1);
    hi = Math.min(points.length - 1, i + 1);
  }
  return [lo, hi];
}

/**
 * The track as scene points with an attitude for each.
 *
 * Recorded angles are used wherever they exist and are sane. Tracks stored
 * before an angle was kept get an estimate instead, flagged per point:
 *   heading  the direction of motion (wrong by the crab, and by the ship's
 *            own motion in the ship frame)
 *   roll     the bank of a coordinated turn at that speed and turn rate
 *   pitch    the flight-path angle plus the angle of attack
 * Drawing those wings-level and nose-on-course instead would look exactly
 * like a recording of a pilot who flew that way.
 */
export function buildFlightPath(
  track: ApproachTrack,
  legTimes: LegTimes,
): FlightPath | null {
  const samples = inShipFrame(track.samples);
  const shipFrame = samples !== track.samples;
  const axisDeg = frameAxisDeg(track, shipFrame);

  const base: Base[] = [];
  for (const s of samples) {
    const along = alongOf(s);
    const lateral = num(s.centerline_deviation);
    const height = num(s.agl);
    if (along === null || lateral === null || height === null) continue;
    base.push({ time: s.time, x: -along, y: height, z: lateral, sample: s });
  }
  base.sort((a, b) => a.time - b.time);
  if (base.length < 2) return null;

  // Direction of motion first: the heading fallback, and the source of the
  // turn rate when the backend did not derive one.
  const motion = base.map((_, i) => {
    const [lo, hi] = neighbours(base, i, DERIVATIVE_HALF_WINDOW_S);
    const a = base[lo];
    const b = base[hi];
    const dx = b.x - a.x;
    const dz = b.z - a.z;
    const horizontal = Math.hypot(dx, dz);
    const dt = b.time - a.time;
    return {
      heading: horizontal > 0.5 ? Math.atan2(dz, dx) / DEG : null,
      speed: dt > 0 ? horizontal / dt : null,
      pathAngle: horizontal > 0.5 ? Math.atan2(b.y - a.y, horizontal) / DEG : null,
    };
  });
  // Carry the last known direction through stationary stretches.
  let lastHeading = 0;
  const motionHeading = motion.map((m) => {
    if (m.heading !== null) lastHeading = m.heading;
    return lastHeading;
  });

  const estimatedCount = { heading: 0, pitch: 0, roll: 0 };
  const points: FlightPoint[] = base.map((p, i) => {
    const s = p.sample;

    const recordedHeading = num(s.heading);
    const heading =
      recordedHeading !== null && axisDeg !== null
        ? wrap180(recordedHeading - axisDeg)
        : motionHeading[i];
    const headingEstimated = recordedHeading === null || axisDeg === null;

    const recordedRoll = num(s.roll);
    let roll: number;
    let rollEstimated = false;
    // Taxiing rows carry corrupt Roll values of 12,000+ deg.
    if (recordedRoll !== null && Math.abs(recordedRoll) <= 180) {
      roll = recordedRoll;
    } else {
      rollEstimated = true;
      let rate = num(s.turn_rate_deg_s);
      if (rate === null) {
        const [lo, hi] = neighbours(base, i, DERIVATIVE_HALF_WINDOW_S);
        const dt = base[hi].time - base[lo].time;
        rate = dt > 0 ? wrap180(motionHeading[hi] - motionHeading[lo]) / dt : 0;
      }
      const speed = num(s.speed) ?? motion[i].speed ?? 0;
      roll = Math.atan((speed * rate * DEG) / G) / DEG;
    }

    const recordedPitch = num(s.pitch);
    let pitch: number;
    let pitchEstimated = false;
    if (recordedPitch !== null && Math.abs(recordedPitch) <= 90) {
      pitch = recordedPitch;
    } else {
      pitchEstimated = true;
      pitch = (motion[i].pathAngle ?? 0) + (num(s.aoa) ?? 0);
    }

    if (headingEstimated) estimatedCount.heading += 1;
    if (rollEstimated) estimatedCount.roll += 1;
    if (pitchEstimated) estimatedCount.pitch += 1;

    return {
      time: p.time,
      x: p.x,
      y: p.y,
      z: p.z,
      heading,
      pitch,
      roll,
      estimated: { heading: headingEstimated, pitch: pitchEstimated, roll: rollEstimated },
      leg: legAt(p.time, legTimes),
      sample: s,
    };
  });

  return { points, axisDeg, shipFrame, estimatedCount };
}

/**
 * Nose, top and right-wing unit vectors in scene axes for an attitude.
 *
 * Heading, then pitch, then roll (intrinsic yaw-pitch-roll, the aviation
 * convention): R = Ry(-heading) * Rz(pitch) * Rx(roll) applied to the body
 * axes nose = +x, up = +y, right = +z.
 */
export function attitudeBasis(
  headingDeg: number,
  pitchDeg: number,
  rollDeg: number,
): { nose: Vec3; up: Vec3; right: Vec3 } {
  const ch = Math.cos(headingDeg * DEG);
  const sh = Math.sin(headingDeg * DEG);
  const cp = Math.cos(pitchDeg * DEG);
  const sp = Math.sin(pitchDeg * DEG);
  const cr = Math.cos(rollDeg * DEG);
  const sr = Math.sin(rollDeg * DEG);
  return {
    nose: [cp * ch, sp, cp * sh],
    up: [-cr * sp * ch - sr * sh, cr * cp, -cr * sp * sh + sr * ch],
    right: [sr * sp * ch - cr * sh, -sr * cp, sr * sp * sh + cr * ch],
  };
}

/**
 * Mission times for the ghost aircraft: every `intervalS` seconds, counted
 * from the touchdown so that one of them always sits on it. Capped so a
 * long recording at a short interval cannot bury the path under models.
 */
export function ghostTimes(
  startS: number,
  endS: number,
  anchorS: number | null,
  intervalS: number,
  maxCount = 400,
): number[] {
  if (!(intervalS > 0) || !(endS >= startS)) return [];
  let step = intervalS;
  while ((endS - startS) / step + 1 > maxCount) step *= 2;
  const anchor = anchorS ?? startS;
  const first = anchor - Math.floor((anchor - startS) / step) * step;
  const times: number[] = [];
  for (let t = first; t <= endS + 1e-9; t += step) times.push(t);
  return times;
}

/** The point nearest in time, if one is within `toleranceS`. */
export function pointAt(
  points: FlightPoint[],
  time: number,
  toleranceS = 1.0,
): FlightPoint | null {
  if (points.length === 0) return null;
  let lo = 0;
  let hi = points.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (points[mid].time <= time) lo = mid;
    else hi = mid;
  }
  const best =
    Math.abs(points[lo].time - time) <= Math.abs(points[hi].time - time)
      ? points[lo]
      : points[hi];
  return Math.abs(best.time - time) <= toleranceS ? best : null;
}

/** Median speed along the path (m/s, in the scene frame). */
export function medianSpeed(points: FlightPoint[]): number {
  const speeds: number[] = [];
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1];
    const b = points[i];
    const dt = b.time - a.time;
    if (dt > 0) speeds.push(Math.hypot(b.x - a.x, b.y - a.y, b.z - a.z) / dt);
  }
  if (speeds.length === 0) return 0;
  speeds.sort((p, q) => p - q);
  return speeds[Math.floor(speeds.length / 2)];
}

/**
 * Length to draw the aircraft model at (m).
 *
 * Large enough to read the attitude of a whole circuit (3% of the scene;
 * real size, 18 m, when zoomed onto a deck), but never longer than the gap
 * between two ghosts, or a slow downwind becomes a solid comb of wings.
 */
export function modelLength(
  span: number,
  speedMs: number,
  ghostIntervalS: number,
  scale = 1,
): number {
  const spacing = speedMs * ghostIntervalS * 0.85;
  const length = Math.min(span * 0.03, Math.max(spacing, span * 0.012));
  return Math.min(Math.max(length, 18), 500) * scale;
}

export interface SceneBounds {
  min: Vec3;
  max: Vec3;
  center: Vec3;
  /** Largest horizontal span (m); drives model size and grid spacing. */
  span: number;
}

/** Bounds of the path, always including the reference point on the ground
 *  so the runway / deck is inside the initial view. */
export function sceneBounds(points: FlightPoint[], minSpanM = 600): SceneBounds {
  const min: Vec3 = [0, 0, 0];
  const max: Vec3 = [0, 0, 0];
  for (const p of points) {
    min[0] = Math.min(min[0], p.x);
    min[1] = Math.min(min[1], p.y);
    min[2] = Math.min(min[2], p.z);
    max[0] = Math.max(max[0], p.x);
    max[1] = Math.max(max[1], p.y);
    max[2] = Math.max(max[2], p.z);
  }
  const span = Math.max(max[0] - min[0], max[2] - min[2], minSpanM);
  return {
    min,
    max,
    center: [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2],
    span,
  };
}

export interface Strip {
  /** Centreline end points on the ground. */
  from: Vec3;
  to: Vec3;
  width: number;
}

export interface VenueGeometry {
  /** The runway, when its real length is known. */
  runway: Strip | null;
  /** The ship's hull (ship frame only) and its angled landing area. */
  hull: Strip | null;
  landingArea: Strip | null;
  /** Ideal glide path, from its end point back along the final. */
  glideslope: { from: Vec3; to: Vec3 } | null;
  /** The landing course on the ground: extended far back down the final,
   *  forward to the end of the runway / landing area. */
  course: { from: Vec3; to: Vec3 };
}

/** Nimitz-class figures, for drawing only: nothing is measured against them. */
const HULL_WIDTH_M = 40;
const LANDING_AREA_WIDTH_M = 25;
const DEFAULT_RUNWAY_WIDTH_M = 45;

/**
 * Where to draw the runway or the ship, and the ideal glide path, in scene
 * coordinates. Mirrors what the plan view draws (PatternTrack).
 *
 * `glideslopeLengthM` is the horizontal length of the glide path drawn,
 * `courseLengthM` how far back down the final the course line runs.
 */
export function venueGeometry(
  track: ApproachTrack,
  shipFrame: boolean,
  glideslopeLengthM: number,
  courseLengthM = 20_000,
): VenueGeometry {
  const geometry = track.geometry ?? {};
  const slope = num(track.glideslope_deg);
  const refHeight = num(geometry["reference_height_m"]) ?? 0;
  const tanSlope = slope !== null ? Math.tan(slope * DEG) : null;

  let runway: Strip | null = null;
  let hull: Strip | null = null;
  let landingArea: Strip | null = null;
  // Glide-path end point and its horizontal direction back down the final.
  let end: Vec3 = [0, refHeight, 0];
  let back: [number, number] = [-1, 0];

  const rampX = num(geometry["ramp_along_m"]);
  const rampY = num(geometry["ramp_lateral_m"]);
  const deckAngle = num(geometry["landing_course_offset_deg"]);
  const deckLength = num(geometry["landing_area_length_m"]);
  const target = num(geometry["touchdown_target_m"]) ?? 0;

  if (shipFrame && rampX !== null && rampY !== null && deckAngle !== null) {
    // Ship frame: origin at the ship's ACMI position, +x up the BRC, the
    // landing area angled `deckAngle` off it from the ramp.
    const c = Math.cos(deckAngle * DEG);
    const s = Math.sin(deckAngle * DEG);
    hull = { from: [rampX, 0, 0], to: [-rampX, 0, 0], width: HULL_WIDTH_M };
    if (deckLength !== null) {
      landingArea = {
        from: [rampX, 0, rampY],
        to: [rampX + deckLength * c, 0, rampY + deckLength * s],
        width: LANDING_AREA_WIDTH_M,
      };
    }
    end = [rampX + target * c, refHeight, rampY + target * s];
    back = [-c, -s];
  } else if (deckLength !== null && track.kind === "carrier") {
    // Landing-area frame (older carrier tracks): origin at the glide path's
    // end point, the ramp `target` metres short of it.
    landingArea = {
      from: [-target, 0, 0],
      to: [-target + deckLength, 0, 0],
      width: LANDING_AREA_WIDTH_M,
    };
  } else {
    const length = num(geometry["length_m"]);
    const aiming = num(geometry["aiming_point_m"]) ?? 0;
    if (length !== null) {
      runway = {
        from: [-aiming, 0, 0],
        to: [length - aiming, 0, 0],
        width: num(geometry["width_m"]) ?? DEFAULT_RUNWAY_WIDTH_M,
      };
    }
  }

  const glideslope =
    tanSlope !== null
      ? {
          from: end,
          to: [
            end[0] + back[0] * glideslopeLengthM,
            end[1] + glideslopeLengthM * tanSlope,
            end[2] + back[1] * glideslopeLengthM,
          ] as Vec3,
        }
      : null;

  // Forward end: the far end of whatever the aircraft lands on, else a
  // kilometre past the reference point.
  const strip = runway ?? landingArea;
  const ahead = strip ? strip.to : [end[0] - back[0] * 1000, 0, end[2] - back[1] * 1000];
  const course = {
    from: [end[0] + back[0] * courseLengthM, 0, end[2] + back[1] * courseLengthM] as Vec3,
    to: [ahead[0], 0, ahead[2]] as Vec3,
  };

  return { runway, hull, landingArea, glideslope, course };
}

/** Grid spacing (m) giving roughly `divisions` cells across `span`, rounded
 *  to 0.1 / 0.25 / 0.5 / 1 / 2 nm. */
export function gridStepM(span: number, divisions = 10): number {
  const steps = [0.1, 0.25, 0.5, 1, 2, 5].map((nm) => nm * 1852);
  const wanted = span / divisions;
  return steps.find((s) => s >= wanted) ?? steps[steps.length - 1];
}
