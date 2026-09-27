import { describe, expect, it } from "vitest";
import {
  attitudeBasis,
  buildFlightPath,
  ghostTimes,
  gridStepM,
  medianSpeed,
  modelLength,
  pointAt,
  venueGeometry,
  wrap180,
  type Vec3,
} from "./flight3d";
import type { ApproachTrack, DeviationSample } from "../types/api";

const DEG = Math.PI / 180;

function dot(a: Vec3, b: Vec3): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

function cross(a: Vec3, b: Vec3): Vec3 {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

/** A straight track at `speed` m/s along +x of the frame, `lateral` m right,
 *  descending at `sink` m/s from `height`; attitude fields from `extra`. */
function straightTrack(
  extra: (i: number) => Partial<DeviationSample>,
  opts: { course?: number; speed?: number; sink?: number; n?: number } = {},
): ApproachTrack {
  const { course = 90, speed = 70, sink = 0, n = 41 } = opts;
  const samples: DeviationSample[] = [];
  for (let i = 0; i < n; i++) {
    const t = 100 + i * 0.2;
    const along = 3000 - speed * (t - 100);
    samples.push({
      time: t,
      distance_to_go: Math.max(0, along),
      signed_distance_to_go: along,
      centerline_deviation: 0,
      agl: 300 - sink * (t - 100),
      speed,
      ...extra(i),
    });
  }
  return { course_deg: course, glideslope_deg: 3, samples };
}

describe("wrap180", () => {
  it("folds angles into (-180, 180]", () => {
    expect(wrap180(359 - 1)).toBe(-2);
    expect(wrap180(-190)).toBe(170);
    expect(wrap180(180)).toBe(180);
    expect(wrap180(-180)).toBe(180);
    expect(wrap180(725)).toBe(5);
  });
});

describe("attitudeBasis", () => {
  it("is the identity for a level aircraft on the axis", () => {
    const b = attitudeBasis(0, 0, 0);
    expect(b.nose).toEqual([1, 0, 0]);
    expect(b.up.map((v) => v + 0)).toEqual([0, 1, 0]);
    expect(b.right.map((v) => v + 0)).toEqual([0, 0, 1]);
  });

  it("recovers every heading, pitch and bank by the standard extraction", () => {
    // Independent oracle: the textbook Euler extraction, which never composes
    // rotations, so a wrong sign or order in the composition cannot cancel.
    for (const h of [-170, -95, -30, 0, 12, 90, 135, 179]) {
      for (const p of [-60, -10, 0, 7, 45, 80]) {
        for (const r of [-150, -60, -20, 0, 25, 90, 170]) {
          const { nose, up, right } = attitudeBasis(h, p, r);
          expect(Math.atan2(nose[2], nose[0]) / DEG).toBeCloseTo(h, 6);
          expect(Math.asin(nose[1]) / DEG).toBeCloseTo(p, 6);
          expect(Math.atan2(-right[1], up[1]) / DEG).toBeCloseTo(r, 6);
          // Orthonormal and right-handed, so it is a rotation and three.js
          // can build a quaternion from it.
          expect(dot(nose, up)).toBeCloseTo(0, 9);
          expect(dot(nose, right)).toBeCloseTo(0, 9);
          const c = cross(nose, up);
          c.forEach((v, i) => expect(v).toBeCloseTo(right[i], 9));
        }
      }
    }
  });

  it("puts the right wing down in a right bank and the nose right on a right heading", () => {
    expect(attitudeBasis(0, 0, 30).right[1]).toBeLessThan(0);
    expect(attitudeBasis(0, 0, 30).up[2]).toBeGreaterThan(0);
    expect(attitudeBasis(90, 0, 0).nose[2]).toBeCloseTo(1, 9);
  });
});

describe("buildFlightPath", () => {
  it("places the track forward along the course with the height above the surface", () => {
    const path = buildFlightPath(straightTrack(() => ({})), {})!;
    const first = path.points[0];
    const last = path.points[path.points.length - 1];
    expect(first.x).toBeCloseTo(-3000, 6);
    expect(first.y).toBe(300);
    expect(last.x).toBeGreaterThan(first.x);
  });

  it("draws the recorded nose, crab included, relative to the course", () => {
    // Ground track due along the course, nose held 4.5 deg right: only the
    // recorded heading can show that.
    const path = buildFlightPath(
      straightTrack(() => ({ heading: 94.5, roll: 0, pitch: 2 }), { course: 90 }),
      {},
    )!;
    for (const p of path.points) {
      expect(p.heading).toBeCloseTo(4.5, 6);
      expect(p.estimated.heading).toBe(false);
    }
    expect(path.estimatedCount).toEqual({ heading: 0, pitch: 0, roll: 0 });
  });

  it("wraps the relative heading across north", () => {
    const path = buildFlightPath(straightTrack(() => ({ heading: 358 }), { course: 3 }), {})!;
    expect(path.points[0].heading).toBeCloseTo(-5, 6);
  });

  it("falls back to the direction of motion when the heading was not kept", () => {
    const samples: DeviationSample[] = [];
    for (let i = 0; i < 20; i++) {
      const t = i * 0.2;
      // 45 deg to the right of the course.
      samples.push({
        time: t,
        distance_to_go: 0,
        signed_distance_to_go: 2000 - 50 * t,
        centerline_deviation: 50 * t,
        agl: 300,
      });
    }
    const path = buildFlightPath({ course_deg: 0, samples }, {})!;
    expect(path.points[10].heading).toBeCloseTo(45, 6);
    expect(path.points[10].estimated.heading).toBe(true);
    expect(path.estimatedCount.heading).toBe(20);
  });

  it("estimates a coordinated-turn bank when roll is missing or corrupt", () => {
    // 3 deg/s at 70 m/s: tan(bank) = 70 * 0.05236 / 9.80665.
    const expected = Math.atan((70 * 3 * DEG) / 9.80665) / DEG;
    expect(expected).toBeCloseTo(20.49, 1);
    for (const roll of [null, 12500]) {
      const path = buildFlightPath(
        straightTrack(() => ({ roll, turn_rate_deg_s: 3, heading: 90, pitch: 0 })),
        {},
      )!;
      expect(path.points[5].roll).toBeCloseTo(expected, 6);
      expect(path.points[5].estimated.roll).toBe(true);
    }
  });

  it("keeps a recorded bank as recorded", () => {
    const path = buildFlightPath(
      straightTrack(() => ({ roll: -62.5, turn_rate_deg_s: 3 })),
      {},
    )!;
    expect(path.points[5].roll).toBe(-62.5);
    expect(path.points[5].estimated.roll).toBe(false);
  });

  it("estimates pitch as the flight-path angle plus the angle of attack", () => {
    // Sinking 3.668 m/s at 70 m/s is a 3 deg glide path.
    const sink = 70 * Math.tan(3 * DEG);
    const path = buildFlightPath(straightTrack(() => ({ aoa: 8.1 }), { sink }), {})!;
    expect(path.points[20].pitch).toBeCloseTo(-3 + 8.1, 6);
    expect(path.points[20].estimated.pitch).toBe(true);
  });

  it("uses the ship's frame and heading for a moving-deck carrier track", () => {
    const samples: DeviationSample[] = [0, 1, 2].map((i) => ({
      time: i,
      distance_to_go: 900 - 70 * i,
      signed_distance_to_go: 900 - 70 * i,
      centerline_deviation: 5,
      agl: 100,
      heading: 95,
      ship_along: -1100 + 70 * i,
      ship_lateral: -160,
    }));
    const track: ApproachTrack = {
      kind: "carrier",
      course_deg: 81,
      geometry: { frame: "moving_deck", ship_heading_deg: 90, landing_course_offset_deg: -9 },
      samples,
    };
    const path = buildFlightPath(track, {})!;
    expect(path.shipFrame).toBe(true);
    expect(path.axisDeg).toBe(90);
    expect(path.points[0].x).toBe(-1100);
    expect(path.points[0].z).toBe(-160);
    expect(path.points[0].heading).toBeCloseTo(5, 6);
  });

  it("returns null when fewer than two samples have a position and a height", () => {
    expect(
      buildFlightPath(
        { samples: [{ time: 0, distance_to_go: 10, centerline_deviation: 0, agl: null }] },
        {},
      ),
    ).toBeNull();
  });
});

describe("ghostTimes", () => {
  it("counts from the touchdown so a ghost always sits on it", () => {
    const times = ghostTimes(100, 130, 120, 3);
    expect(times[0]).toBe(102);
    expect(times).toContain(120);
    expect(times[times.length - 1]).toBe(129);
    for (let i = 1; i < times.length; i++) expect(times[i] - times[i - 1]).toBeCloseTo(3, 9);
  });

  it("works with the anchor outside the recording", () => {
    expect(ghostTimes(100, 110, 50, 4)).toEqual([102, 106, 110]);
  });

  it("widens the interval rather than drawing an unbounded number of models", () => {
    const times = ghostTimes(0, 1000, 500, 1, 100);
    expect(times.length).toBeLessThanOrEqual(100);
    expect(times).toContain(500);
  });

  it("returns nothing for a non-positive interval", () => {
    expect(ghostTimes(0, 10, 5, 0)).toEqual([]);
  });
});

describe("pointAt", () => {
  const path = buildFlightPath(straightTrack(() => ({})), {})!;

  it("finds the nearest point in time", () => {
    expect(pointAt(path.points, 101.03)!.time).toBeCloseTo(101.0, 9);
  });

  it("returns null beyond the tolerance", () => {
    expect(pointAt(path.points, 150, 1)).toBeNull();
  });
});

describe("venueGeometry", () => {
  it("draws the runway from its threshold, short of the aiming point", () => {
    const v = venueGeometry(
      { course_deg: 90, glideslope_deg: 3, geometry: { kind: "runway", length_m: 2500, width_m: 50, aiming_point_m: 300 }, samples: [] },
      false,
      2000,
    );
    expect(v.runway).toEqual({ from: [-300, 0, 0], to: [2200, 0, 0], width: 50 });
    // The course line runs from far down the final to the runway's far end.
    expect(v.course).toEqual({ from: [-20000, 0, 0], to: [2200, 0, 0] });
    expect(v.glideslope!.to[0]).toBe(-2000);
    expect(v.glideslope!.to[1]).toBeCloseTo(2000 * Math.tan(3 * DEG), 9);
  });

  it("runs a carrier's glide path down the angled deck in the ship frame", () => {
    const v = venueGeometry(
      {
        kind: "carrier",
        glideslope_deg: 3.5,
        geometry: {
          frame: "moving_deck",
          ramp_along_m: -160,
          ramp_lateral_m: -10,
          landing_course_offset_deg: -9,
          landing_area_length_m: 220,
          touchdown_target_m: 60,
          reference_height_m: 4,
        },
        samples: [],
      },
      true,
      1000,
    );
    const { from, to } = v.glideslope!;
    // The glide path ends on the landing-area centreline, `target` up it.
    expect(from[0]).toBeCloseTo(-160 + 60 * Math.cos(-9 * DEG), 9);
    expect(from[2]).toBeCloseTo(-10 + 60 * Math.sin(-9 * DEG), 9);
    expect(from[1]).toBe(4);
    // ...and comes in from astern along the angled deck, i.e. from the
    // starboard quarter for a deck angled to port.
    const bearing = Math.atan2(from[2] - to[2], from[0] - to[0]) / DEG;
    expect(bearing).toBeCloseTo(-9, 6);
    expect(Math.hypot(to[0] - from[0], to[2] - from[2])).toBeCloseTo(1000, 6);
    expect(v.hull!.from[0]).toBe(-160);
    expect(v.runway).toBeNull();
    // The course line is the angled deck's, astern to the deck's forward end.
    const course = Math.atan2(
      v.course.to[2] - v.course.from[2],
      v.course.to[0] - v.course.from[0],
    );
    expect(course / DEG).toBeCloseTo(-9, 6);
    expect(v.course.to).toEqual([v.landingArea!.to[0], 0, v.landingArea!.to[2]]);
  });
});

describe("modelLength", () => {
  it("never draws a model longer than the gap between two ghosts", () => {
    // A 70 m/s downwind at 2 s: ghosts 140 m apart on a 5 km scene, where
    // 3% of the scene would be 150 m.
    const length = modelLength(5_000, 70, 2);
    expect(length).toBeLessThanOrEqual(140);
    expect(length).toBeCloseTo(70 * 2 * 0.85, 9);
    expect(modelLength(5_000, 70, 2, 1.6)).toBeCloseTo(70 * 2 * 0.85 * 1.6, 9);
  });

  it("stays readable when the ghosts are close, and real-size on a deck", () => {
    // 1.2% of the scene is the floor, whatever the interval.
    expect(modelLength(10_000, 70, 0.5)).toBeCloseTo(120, 9);
    expect(modelLength(300, 60, 2)).toBe(18);
  });

  it("measures the speed along the path", () => {
    const path = buildFlightPath(straightTrack(() => ({}), { speed: 70 }), {})!;
    expect(medianSpeed(path.points)).toBeCloseTo(70, 6);
  });
});

describe("gridStepM", () => {
  it("picks a round nautical-mile step", () => {
    expect(gridStepM(10_000)).toBeCloseTo(1852, 6);
    expect(gridStepM(1_000)).toBeCloseTo(185.2, 6);
  });
});
