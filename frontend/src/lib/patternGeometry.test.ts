import { describe, expect, it } from "vitest";
import {
  alongOf,
  downwindGuide,
  legAt,
  legRuns,
  M_PER_NM,
  patternProjection,
  scaleBarLabel,
} from "./patternGeometry";
import type { DeviationSample } from "../types/api";

function sample(
  time: number,
  along: number,
  lateral: number,
  signed = true,
): DeviationSample {
  return {
    time,
    distance_to_go: Math.max(0, along),
    signed_distance_to_go: signed ? along : undefined,
    centerline_deviation: lateral,
  };
}

describe("alongOf", () => {
  it("uses the signed value so the upwind side does not fold onto the threshold", () => {
    expect(alongOf(sample(0, -800, 0))).toBe(-800);
  });

  it("falls back to the clamped value for tracks recorded before it existed", () => {
    expect(alongOf(sample(0, -800, 0, false))).toBe(0);
  });
});

describe("patternProjection", () => {
  const circuit = [
    sample(0, -900, 1800),
    sample(10, 400, 1800),
    sample(20, 1800, 1800),
    sample(30, 2400, 900),
    sample(40, 2200, 0),
    sample(50, 1000, 0),
    sample(60, 0, 0),
  ];

  it("uses one scale for both axes so turns keep their shape", () => {
    const p = patternProjection(circuit, {}, 400, 400, 20)!;
    expect(p).not.toBeNull();
    // Equal metre steps must move the same number of pixels on both axes.
    const origin = p.toPx(0, 0);
    const alongStep = p.toPx(1000, 0);
    const lateralStep = p.toPx(0, 1000);
    expect(Math.abs(alongStep.py - origin.py)).toBeCloseTo(
      Math.abs(lateralStep.px - origin.px),
      6,
    );
  });

  it("puts the runway end above the far end of the approach", () => {
    // The aircraft flies UP the page, like every pattern diagram.
    const p = patternProjection(circuit, {}, 400, 400, 20)!;
    expect(p.toPx(0, 0).py).toBeLessThan(p.toPx(2400, 0).py);
  });

  it("keeps right-of-course on the right of the page", () => {
    const p = patternProjection(circuit, {}, 400, 400, 20)!;
    expect(p.toPx(0, 500).px).toBeGreaterThan(p.toPx(0, -500).px);
  });

  it("fits the whole circuit inside the frame", () => {
    const p = patternProjection(circuit, {}, 400, 400, 20)!;
    for (const point of p.points) {
      expect(point.px).toBeGreaterThanOrEqual(0);
      expect(point.px).toBeLessThanOrEqual(400);
      expect(point.py).toBeGreaterThanOrEqual(0);
      expect(point.py).toBeLessThanOrEqual(400);
    }
  });

  it("picks a scale bar that fits", () => {
    const p = patternProjection(circuit, {}, 400, 400, 20)!;
    expect(p.scaleBarM / p.metersPerPx).toBeLessThanOrEqual(400);
    expect(p.scaleBarM).toBeGreaterThan(0);
  });

  it("returns null when there is nothing plottable", () => {
    expect(patternProjection([], {}, 400, 400, 20)).toBeNull();
  });
});

describe("legAt", () => {
  const times = {
    downwindStart: 0,
    downwindEnd: 20,
    rollout: 40,
    touchdown: 60,
  };

  it("names each leg of the circuit", () => {
    expect(legAt(-5, times)).toBe("entry");
    expect(legAt(10, times)).toBe("downwind");
    expect(legAt(30, times)).toBe("base");
    expect(legAt(50, times)).toBe("final");
    expect(legAt(70, times)).toBe("rollout");
  });

  it("calls everything final when the backend reported no pattern", () => {
    // Straight-in, or a track stored before the boundaries were computed:
    // inventing legs there would be a lie drawn in colour.
    expect(legAt(10, {})).toBe("final");
  });
});

describe("legRuns", () => {
  it("repeats the boundary point so the polylines join", () => {
    const points = [
      { px: 0, py: 0, alongM: 0, lateralM: 0, time: 0, leg: "downwind" as const },
      { px: 1, py: 1, alongM: 10, lateralM: 0, time: 1, leg: "downwind" as const },
      { px: 2, py: 2, alongM: 20, lateralM: 0, time: 2, leg: "base" as const },
    ];
    const runs = legRuns(points);
    expect(runs.map((r) => r.leg)).toEqual(["downwind", "base"]);
    expect(runs[1].points[0]).toBe(points[1]);
  });
});

describe("scaleBarLabel", () => {
  it("reads in nautical miles", () => {
    expect(scaleBarLabel(M_PER_NM)).toBe("1 nm");
    expect(scaleBarLabel(0.5 * M_PER_NM)).toBe("0.5 nm");
  });
});

describe("downwindGuide", () => {
  const legTimes = { downwindStart: 0, downwindEnd: 20, rollout: 40, touchdown: 60 };
  const circuit = [
    sample(0, -900, 1800),
    sample(10, 400, 1750),
    sample(20, 1800, 1700),
    sample(30, 2400, 900),
    sample(40, 2200, 0),
    sample(60, 0, 0),
  ];

  function project() {
    return patternProjection(circuit, legTimes, 400, 400, 20)!;
  }

  it("draws the fitted leg tilted against a runway-parallel reference", () => {
    const p = project();
    const guide = downwindGuide(p.points, -8, p.toPx)!;
    expect(guide).not.toBeNull();
    // The reference is parallel to the runway: constant lateral, so in the
    // plan view it runs straight along the course axis.
    const idealDx = guide.ideal.x2 - guide.ideal.x1;
    const actualDx = guide.actual.x2 - guide.actual.x1;
    expect(Math.abs(idealDx)).toBeLessThan(0.01);
    expect(Math.abs(actualDx)).toBeGreaterThan(1);
  });

  it("tilts the fitted line the other way for the opposite sign", () => {
    const p = project();
    const left = downwindGuide(p.points, -8, p.toPx)!;
    const right = downwindGuide(p.points, 8, p.toPx)!;
    const leftDx = left.actual.x2 - left.actual.x1;
    const rightDx = right.actual.x2 - right.actual.x1;
    expect(Math.sign(leftDx)).toBe(-Math.sign(rightDx));
  });

  it("draws nothing when the backend reported no downwind angle", () => {
    // The angle is the backend's; refitting it here would let the picture
    // and the score disagree about the same named number.
    const p = project();
    expect(downwindGuide(p.points, null, p.toPx)).toBeNull();
    expect(downwindGuide(p.points, undefined, p.toPx)).toBeNull();
  });

  it("draws nothing when there is no downwind leg", () => {
    const p = patternProjection(circuit, {}, 400, 400, 20)!;
    expect(downwindGuide(p.points, -8, p.toPx)).toBeNull();
  });
});
