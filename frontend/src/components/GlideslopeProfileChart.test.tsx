/**
 * @vitest-environment jsdom
 *
 * Recharts validates its axis wiring while mounting, and an inconsistency
 * throws rather than degrading: a `yAxisId` on a <Line> with no matching
 * <YAxis> raises "Invariant failed", which unmounts the whole React tree and
 * leaves the detail page blank. That shipped once already, so the chart is
 * mounted here for real.
 *
 * The chart is drawn at a fixed pixel size rather than in a
 * ResponsiveContainer -- the container measures its parent on mount and does
 * not re-measure for print, so the printed sheet got a chart squeezed into a
 * third of the page. Nothing to stub as a result.
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";

const { GlideslopeProfileChart } = await import("./GlideslopeProfileChart");

function track(overrides: Record<string, unknown> = {}) {
  return {
    kind: "land",
    outcome: "full_stop",
    glideslope_deg: 3.0,
    course_deg: 220.0,
    touchdown_time: 100,
    samples: Array.from({ length: 24 }, (_, i) => ({
      time: 76 + i,
      distance_to_go: 2300 - i * 100,
      glideslope_deviation: -12 + i,
      centerline_deviation: 8 - i * 0.4,
      speed: 70,
      aoa: null,
      agl: 120 - i * 5,
    })),
    ...overrides,
  };
}

describe("GlideslopeProfileChart, carrier", () => {
  const movingDeck = { frame: "moving_deck", reference_height_m: 2 };

  it("calls the final the groove and measures to the target wire", () => {
    const { container } = render(
      <GlideslopeProfileChart
        track={track({ kind: "carrier", geometry: movingDeck })}
        metrics={{ pattern_rollout_time: 80 }}
      />,
    );
    expect(container.textContent).toContain("グルーブ開始");
    expect(container.textContent).toContain("目標ワイヤー");
    expect(container.textContent).not.toContain("ベース→ファイナル");
  });

  it("keeps the old wording for carrier rows stored before the deck was tracked", () => {
    // No `frame`: ramp- or touchdown-referenced, sea-referenced height. Saying
    // "target wire, height above the deck" about those would be false.
    const { container } = render(
      <GlideslopeProfileChart
        track={track({ kind: "carrier", geometry: { key: "stennis", landing_course_offset_deg: 9 } })}
        metrics={{ pattern_rollout_time: 80 }}
      />,
    );
    expect(container.textContent).not.toContain("目標ワイヤー");
    expect(container.textContent).toContain("接地点までの距離");
  });

  it("ends the ideal glideslope where the deck's glideslope ends: 2 m up", async () => {
    const { buildProfilePoints } = await import("./GlideslopeProfileChart");
    const atWire = { time: 100, distance_to_go: 0, agl: 2 };
    const [carrierEnd] = buildProfilePoints(
      track({ kind: "carrier", geometry: movingDeck, samples: [atWire] }) as never,
    );
    const [landEnd] = buildProfilePoints(track({ samples: [atWire] }) as never);
    expect(carrierEnd.ideal_ft).toBeCloseTo(2 / 0.3048, 3);
    expect(landEnd.ideal_ft).toBe(0);
  });
});

describe("GlideslopeProfileChart", () => {
  it("mounts with every axis its lines reference", () => {
    const { container } = render(
      <GlideslopeProfileChart track={track() as never} />,
    );
    // Both the altitude and the deviation axes have to exist, or Recharts
    // throws on mount instead of rendering.
    expect(container.querySelectorAll(".recharts-yAxis").length).toBe(2);
    expect(container.querySelector(".recharts-line")).not.toBeNull();
  });

  it("survives samples with missing deviations and altitudes", () => {
    const sparse = track({
      samples: [
        { time: 80, distance_to_go: 1800, glideslope_deviation: null,
          centerline_deviation: null, speed: null, aoa: null, agl: null },
        { time: 90, distance_to_go: 900, glideslope_deviation: -4,
          centerline_deviation: 2, speed: 70, aoa: null, agl: 46 },
      ],
    });
    expect(() =>
      render(<GlideslopeProfileChart track={sparse as never} />),
    ).not.toThrow();
  });

  it("reports missing data instead of mounting an empty chart", () => {
    const { container } = render(
      <GlideslopeProfileChart track={track({ samples: [] }) as never} />,
    );
    expect(container.querySelector(".recharts-yAxis")).toBeNull();
    expect(container.textContent).toContain("データがありません");
  });
});

describe("overhead patterns", () => {
  /** Distance-to-go rises while the aircraft turns onto final, then falls.
   *  Taken from the shape of a real overhead break: out to ~2.1 nm, then in. */
  const overhead = () => ({
    kind: "land", outcome: "full_stop", glideslope_deg: 3.0,
    course_deg: 221.8, touchdown_time: 100,
    samples: [
      ...[3061, 3368, 3625, 3816, 3927, 3962].map((d, i) => ({
        time: 40 + i * 3, distance_to_go: d, glideslope_deviation: 0,
        centerline_deviation: 2200 - i * 300, speed: 90, aoa: null,
        agl: 456 - i * 20,
      })),
      ...[3909, 3300, 2724, 1872, 1036, 472, 0].map((d, i) => ({
        time: 58 + i * 6, distance_to_go: d, glideslope_deviation: 0,
        centerline_deviation: 700 - i * 100, speed: 85, aoa: null,
        agl: 299 - i * 42,
      })),
    ],
  });

  it("plots only the inbound leg, so the line cannot double back", () => {
    const { container } = render(
      <GlideslopeProfileChart track={overhead() as never} />,
    );
    const points =
      container
        .querySelector(".recharts-line-curve")
        ?.getAttribute("d")
        ?.match(/[ML]\s*([\d.]+)/g) ?? [];
    const xs = points.map((m) => parseFloat(m.replace(/[ML]\s*/, "")));
    expect(xs.length).toBeGreaterThan(2);
    // The x axis is reversed (touchdown on the right), so a well-formed
    // approach walks strictly one way across the plot. The outbound half of
    // the pattern would reverse direction partway through.
    const increasing = xs.every((x, i) => i === 0 || x >= xs[i - 1]);
    const decreasing = xs.every((x, i) => i === 0 || x <= xs[i - 1]);
    expect(increasing || decreasing).toBe(true);
  });
});

describe("rolloutDistanceNm", () => {
  it("marks the roll-out where the turn onto final ended", async () => {
    const { rolloutDistanceNm } = await import("./GlideslopeProfileChart");
    const t = track();
    // The fixture's samples run t=76..99 with touchdown at 100.
    const nm = rolloutDistanceNm(t, { pattern_rollout_time: 86 });
    expect(nm).not.toBeNull();
    expect(nm as number).toBeGreaterThan(0);
  });

  it("falls back to the graded final's start for a straight-in", async () => {
    const { rolloutDistanceNm } = await import("./GlideslopeProfileChart");
    const t = track();
    expect(rolloutDistanceNm(t, { final_window_s: 14 })).not.toBeNull();
  });

  it("returns null rather than guessing when the moment is not in the record", async () => {
    const { rolloutDistanceNm } = await import("./GlideslopeProfileChart");
    const t = track();
    expect(rolloutDistanceNm(t, {})).toBeNull();
    expect(rolloutDistanceNm(t, { pattern_rollout_time: 10 })).toBeNull();
  });
});

describe("GlideslopeProfileChart, resolved runway", () => {
  const TAN3 = Math.tan((3 * Math.PI) / 180);
  /** A 3.0 deg path aimed 129 m past the threshold, on a runway whose ILS
   *  glidepath meets it 206 m past the threshold: the production case. The
   *  reference origin is 206 m past the threshold, so distance_to_go is the
   *  threshold distance plus 206. */
  const runway = (overrides: Record<string, unknown> = {}) => {
    const samples = Array.from({ length: 20 }, (_, i) => {
      const toThreshold = 2000 - i * 120; // 2000 m out .. 280 m past the threshold
      const agl = Math.max(0, (toThreshold - 129) * TAN3);
      return {
        time: 80 + i,
        distance_to_go: Math.max(0, toThreshold + 206),
        signed_distance_to_go: toThreshold + 206,
        distance_to_threshold: toThreshold,
        glideslope_deviation: agl - Math.max(0, toThreshold + 206) * TAN3,
        centerline_deviation: 0,
        speed: 70,
        aoa: null,
        agl,
      };
    });
    return {
      kind: "land",
      outcome: "full_stop",
      glideslope_deg: 3.0,
      course_deg: 76.0,
      touchdown_time: 97.5,
      geometry: {
        kind: "runway",
        airbase: "KOBULETI",
        name: "07",
        aiming_point_m: 206.0,
        aiming_point_source: "ils",
        glidepath_intercept_m: 206.0,
        length_m: 2238.2,
        width_m: 60.0,
      },
      samples,
      ...overrides,
    };
  };
  const metrics = { mean_path_angle_deg: 3.0, glidepath_aim_point_from_threshold_m: 129.0 };

  it("measures the x axis to the threshold and names the reference point", () => {
    const { container } = render(
      <GlideslopeProfileChart track={runway() as never} metrics={metrics} />,
    );
    expect(container.textContent).toContain("滑走路末端（閾値）までの距離");
    expect(container.textContent).toContain("基準点 閾値+206 m（DCS の ILS）");
    expect(container.textContent).toContain("飛んだ直線（3.0°、閾値+129 m）");
    expect(container.textContent).not.toContain("接地点までの距離");
  });

  it("brings the reference and the flown line down to where each meets the runway", async () => {
    const { buildProfilePoints } = await import("./GlideslopeProfileChart");
    const points = buildProfilePoints(runway() as never, metrics);
    // x is the threshold distance: the first sample sits 2000 m out.
    expect(points[0].distance_nm).toBeCloseTo(2000 / 1852, 3);
    // The reference line is a 3 deg path through x = -206 m, so 80 m before
    // the threshold it stands (80 + 206) m * tan 3 deg up; the flown line,
    // aimed at +129 m, has already reached the ground there.
    const near = points.find((p) => Math.abs(p.distance_nm - 80 / 1852) < 1e-6);
    expect(near).toBeDefined();
    expect(near!.ideal_ft).toBeCloseTo((286 * TAN3) / 0.3048, 1);
    expect(near!.flown_ft).toBe(0);
    // Far out the two are parallel: the gap is the aim-point difference
    // (206 - (-129)... the reference origin sits at -206 m on this axis and
    // the aim point at +129 m) times tan 3 deg, at every distance. That is
    // what "aimed short, not flown low" looks like.
    const farOut = points[0];
    expect(farOut.ideal_ft! - farOut.flown_ft!).toBeCloseTo(((206 + 129) * TAN3) / 0.3048, 1);
    // Samples past the touchdown time (the ground roll) are not plotted: the
    // trace ends at the last sample before touchdown, 40 m onto the runway.
    expect(points.length).toBe(18);
    expect(points.at(-1)!.distance_nm).toBeCloseTo(-40 / 1852, 4);
  });

  it("keeps the touchdown frame for runway rows stored before threshold distances existed", () => {
    const old = runway({
      samples: runway().samples.map(({ distance_to_threshold: _d, ...s }) => s),
    });
    const { container } = render(<GlideslopeProfileChart track={old as never} />);
    expect(container.textContent).toContain("接地点までの距離");
    expect(container.textContent).not.toContain("飛んだ直線");
  });

  it("marks the roll-out in the same frame as the trace", async () => {
    const { rolloutDistanceNm } = await import("./GlideslopeProfileChart");
    // Sample at t=85 sits 1400 m from the threshold.
    expect(rolloutDistanceNm(runway() as never, { pattern_rollout_time: 85 })).toBeCloseTo(
      1400 / 1852,
      3,
    );
  });
});
