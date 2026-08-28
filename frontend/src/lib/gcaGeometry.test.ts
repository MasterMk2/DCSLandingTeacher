import { describe, expect, it } from "vitest";
import {
  azimuthTrackPoints,
  computeMaxDeviation,
  computeMaxRange,
  descentRateSeries,
  distanceRings,
  elevationTrackPoints,
  niceCeil,
  scopeScaleX,
  scopeScaleY,
  topDownPoints,
} from "./gcaGeometry";
import type { DeviationSample } from "../types/api";

function sample(partial: Partial<DeviationSample>): DeviationSample {
  return {
    time: 0,
    distance_to_go: 0,
    ...partial,
  };
}

describe("niceCeil", () => {
  it("rounds up to a nice value", () => {
    expect(niceCeil(1.2)).toBe(2);
    expect(niceCeil(3.7)).toBe(5);
    expect(niceCeil(6)).toBe(10);
    expect(niceCeil(42)).toBe(50);
    expect(niceCeil(120)).toBe(200);
  });

  it("returns 1 for non-positive input", () => {
    expect(niceCeil(0)).toBe(1);
    expect(niceCeil(-5)).toBe(1);
  });
});

describe("scopeScaleX", () => {
  it("centers zero deviation", () => {
    expect(scopeScaleX(0, 100)).toBe(0.5);
  });

  it("maps +maxDev to the right edge and -maxDev to the left edge", () => {
    expect(scopeScaleX(100, 100)).toBe(1);
    expect(scopeScaleX(-100, 100)).toBe(0);
  });

  it("clamps beyond the scale", () => {
    expect(scopeScaleX(150, 100)).toBe(1);
    expect(scopeScaleX(-150, 100)).toBe(0);
  });

  it("returns center for a degenerate scale", () => {
    expect(scopeScaleX(10, 0)).toBe(0.5);
  });
});

describe("scopeScaleY", () => {
  it("puts far range at the top and touchdown at the bottom", () => {
    expect(scopeScaleY(3000, 3000)).toBe(0);
    expect(scopeScaleY(0, 3000)).toBe(1);
    expect(scopeScaleY(1500, 3000)).toBeCloseTo(0.5);
  });

  it("clamps negative distance-to-go to the bottom", () => {
    expect(scopeScaleY(-10, 3000)).toBe(1);
  });
});

describe("computeMaxDeviation / computeMaxRange", () => {
  it("applies the floor when deviations are tiny", () => {
    const samples = [sample({ centerline_deviation: 2 }), sample({ centerline_deviation: -3 })];
    expect(computeMaxDeviation(samples, (s) => s.centerline_deviation, 30)).toBe(30);
  });

  it("uses the max absolute deviation with nice rounding", () => {
    const samples = [
      sample({ glideslope_deviation: -38 }),
      sample({ glideslope_deviation: 41 }),
      sample({ glideslope_deviation: null }),
    ];
    expect(computeMaxDeviation(samples, (s) => s.glideslope_deviation, 30)).toBe(50);
  });

  it("floors the range at 100 m (Issue #34: low floor preserves short-final precision)", () => {
    expect(computeMaxRange([sample({ distance_to_go: 120 })])).toBe(200);
    expect(computeMaxRange([sample({ distance_to_go: 3700 })])).toBe(5000);
  });

  it("does not force a coarse 30 m deviation scale (Issue #34)", () => {
    // A precise +/-3 m approach used to be zoomed out to a 30 m scale; with the
    // low floor it now scales to a 5 m axis (niceCeil keeps the label round).
    const samples = [sample({ centerline_deviation: 2 }), sample({ centerline_deviation: -3 })];
    expect(computeMaxDeviation(samples, (s) => s.centerline_deviation)).toBe(5);
  });
});

describe("track point projection", () => {
  const samples = [
    sample({ distance_to_go: 3000, centerline_deviation: 50, glideslope_deviation: -25 }),
    sample({ distance_to_go: 1500, centerline_deviation: -25, glideslope_deviation: 25 }),
    sample({ distance_to_go: 0, centerline_deviation: null, glideslope_deviation: null }),
  ];

  it("azimuth points skip null centerline samples", () => {
    const pts = azimuthTrackPoints(samples, 100, 3000);
    expect(pts).toHaveLength(2);
    expect(pts[0]).toEqual({ x: 0.75, y: 0 });
    expect(pts[1]).toEqual({ x: 0.375, y: 0.5 });
  });

  it("elevation points skip null glideslope samples", () => {
    const pts = elevationTrackPoints(samples, 100, 3000);
    expect(pts).toHaveLength(2);
    expect(pts[0].x).toBe(0.375); // below slope -> left of center
    expect(pts[1].x).toBe(0.625);
  });

  it("top-down points use lateral offset in the course frame", () => {
    const pts = topDownPoints(samples, 100, 3000);
    expect(pts).toHaveLength(2);
    expect(pts[0]).toEqual({ x: 0.75, y: 0 });
  });
});

describe("distanceRings", () => {
  it("returns steps strictly below maxRange", () => {
    expect(distanceRings(3700, 1000)).toEqual([1000, 2000, 3000]);
    expect(distanceRings(500, 250)).toEqual([250]);
    expect(distanceRings(200, 250)).toEqual([]);
  });
});

describe("descentRateSeries", () => {
  it("computes positive rates while descending", () => {
    const rows = [
      sample({ time: 0, agl: 100 }),
      sample({ time: 1, agl: 95 }),
      sample({ time: 2, agl: 88 }),
    ];
    const rates = descentRateSeries(rows);
    expect(rates).toEqual([
      { time: 1, rateMs: 5 },
      { time: 2, rateMs: 7 },
    ]);
  });

  it("skips samples without AGL or non-positive dt", () => {
    const rows = [
      sample({ time: 0, agl: null }),
      sample({ time: 1, agl: 90 }),
      sample({ time: 1, agl: 80 }),
      sample({ time: 2, agl: 70 }),
    ];
    const rates = descentRateSeries(rows);
    // (t=0 -> t=1) skipped for null; (t=1 -> t=1) skipped for dt<=0.
    expect(rates).toEqual([{ time: 2, rateMs: 10 }]);
  });
});
