/**
 * @vitest-environment jsdom
 *
 * The geometry is unit-tested separately; this mounts the component so a
 * bad SVG attribute (NaN in a `points` list, a missing projection) fails
 * here instead of blanking the detail page at runtime.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PatternTrack } from "./PatternTrack";
import type { ApproachTrack } from "../types/api";

function circuit(signed: boolean): ApproachTrack {
  // downwind -> base -> final, with the break still past the threshold.
  const rows: [number, number, number][] = [
    [0, -900, 1800],
    [10, 400, 1800],
    [20, 1800, 1800],
    [30, 2400, 900],
    [40, 2200, 0],
    [50, 1000, 0],
    [60, 0, 0],
  ];
  return {
    kind: "land",
    outcome: "full_stop",
    glideslope_deg: 3.0,
    course_deg: 67.2,
    touchdown_time: 60,
    geometry: { kind: "runway", length_m: 2500, aiming_point_m: 300 },
    samples: rows.map(([time, along, lateral]) => ({
      time,
      distance_to_go: Math.max(0, along),
      signed_distance_to_go: signed ? along : undefined,
      centerline_deviation: lateral,
      agl: 300,
    })),
  };
}

const METRICS = {
  pattern_rollout_time: 40,
  pattern_downwind_start_time: 0,
  pattern_downwind_end_time: 20,
  pattern_downwind_course_offset_deg: -7.2,
};

/** A Case I, already in the ship's frame the API serves (x fwd, y stbd). */
function caseOne(): ApproachTrack {
  const rows: [number, number, number][] = [
    [0, -5000, 0], // initial, 3 nm astern
    [30, 1200, 0], // the kiss-off
    [40, 1200, -1042], // halfway round the break
    [50, 1200, -2084], // downwind
    [60, -160, -2084], // abeam the ramp
    [80, -1165, -2084], // start of the 180
    [95, -2285, -964], // the 90
    [110, -1740, 0], // the wake
    [120, -985, 142], // groove
    [136, -84, -3], // trap
  ];
  return {
    kind: "carrier",
    outcome: "full_stop",
    glideslope_deg: 3.5,
    course_deg: 30.9,
    touchdown_time: 136,
    geometry: {
      frame: "moving_deck",
      ramp_along_m: -162.49,
      ramp_lateral_m: 9.38,
      landing_course_offset_deg: -9.1359,
      landing_area_length_m: 250,
      reference_height_m: 2,
    },
    samples: rows.map(([time, x, y]) => ({
      time,
      // The stored frame is the angled deck's; the plan view must not use it.
      distance_to_go: 999,
      signed_distance_to_go: 999,
      centerline_deviation: 999,
      ship_along: x,
      ship_lateral: y,
      agl: 200,
    })),
  };
}

const CASE_ONE_METRICS = {
  pattern_break_start_time: 30,
  pattern_break_end_time: 50,
  pattern_downwind_start_time: 50,
  pattern_downwind_end_time: 80,
  pattern_rollout_time: 120,
  pattern_abeam_time: 60,
  pattern_ninety_time: 95,
  pattern_wake_time: 110,
  pattern_groove_start_time: 120,
  pattern_downwind_course_offset_deg: 0,
};

function markDot(container: HTMLElement, label: string): Element {
  const mark = Array.from(container.querySelectorAll(".pattern-mark")).find(
    (g) => g.querySelector("text")?.textContent === label,
  );
  expect(mark, label).toBeTruthy();
  return mark!.querySelector("circle")!;
}

describe("PatternTrack, carrier", () => {
  it("draws a Case I up the ship's heading, with the ship and its checkpoints", () => {
    const { container } = render(
      <PatternTrack track={caseOne()} metrics={CASE_ONE_METRICS} />,
    );
    for (const line of Array.from(container.querySelectorAll("polyline, line, circle"))) {
      for (const name of ["points", "x1", "x2", "y1", "y2", "cx", "cy"]) {
        expect(line.getAttribute(name) ?? "").not.toMatch(/NaN|Infinity/);
      }
    }
    expect(container.querySelector(".pattern-ship")).not.toBeNull();
    expect(container.querySelector(".pattern-runway")).not.toBeNull();
    expect(
      Array.from(container.querySelectorAll(".pattern-mark text")).map((t) => t.textContent),
    ).toEqual(["キスオフ", "アビーム", "90", "ウェイク", "グルーブ"]);
    expect(container.querySelector(".pattern-legend")?.textContent).toContain(
      "ブレイク（キスオフ）",
    );
    expect(container.textContent).toContain("艦首方向（BRC）");
  });

  it("does not call the turn after a bolter or a wave-off a kiss-off", () => {
    const { container } = render(
      <PatternTrack
        track={caseOne()}
        // A mark is drawn only where the record has a sample within a second
        // of its time, so the wave-off sits on one of the fixture's samples.
        metrics={{ ...CASE_ONE_METRICS, pattern_entry: "turn", pattern_low_pass_time: 0 }}
      />,
    );
    const labels = Array.from(container.querySelectorAll(".pattern-mark text")).map(
      (t) => t.textContent,
    );
    expect(labels).toContain("旋回開始");
    expect(labels).toContain("ウェーブオフ");
    expect(labels).not.toContain("キスオフ");
    const legend = container.querySelector(".pattern-legend")?.textContent ?? "";
    expect(legend).toContain("ダウンウィンドへの旋回");
    expect(legend).not.toContain("キスオフ");
  });

  it("puts the kiss-off ahead of the ship and the downwind to port", () => {
    const { container } = render(
      <PatternTrack track={caseOne()} metrics={CASE_ONE_METRICS} />,
    );
    const ship = container.querySelector(".pattern-ship")!;
    const shipMidY = (Number(ship.getAttribute("y1")) + Number(ship.getAttribute("y2"))) / 2;
    const shipX = Number(ship.getAttribute("x1"));
    // Up the page is forward; left of the page is port.
    expect(Number(markDot(container, "キスオフ").getAttribute("cy"))).toBeLessThan(shipMidY);
    expect(Number(markDot(container, "アビーム").getAttribute("cx"))).toBeLessThan(shipX);
  });
});

describe("PatternTrack", () => {
  it("draws one polyline per leg, with finite coordinates", () => {
    const { container } = render(
      <PatternTrack track={circuit(true)} metrics={METRICS} />,
    );
    const lines = Array.from(container.querySelectorAll("polyline"));
    expect(lines.length).toBeGreaterThan(1);
    for (const line of lines) {
      expect(line.getAttribute("points")).not.toMatch(/NaN|Infinity/);
    }
    expect(container.querySelector(".pattern-leg-downwind")).not.toBeNull();
    expect(container.querySelector(".pattern-leg-final")).not.toBeNull();
  });

  it("shows the downwind heading against a runway-parallel reference", () => {
    const { container } = render(
      <PatternTrack track={circuit(true)} metrics={METRICS} />,
    );
    expect(container.querySelector(".pattern-downwind-fit")).not.toBeNull();
    expect(container.querySelector(".pattern-downwind-ideal")).not.toBeNull();
    expect(container.querySelector(".pattern-downwind-label")?.textContent).toContain(
      "7.2",
    );
  });

  it("omits the downwind guide when the backend reported no angle", () => {
    const { container } = render(
      <PatternTrack
        track={circuit(true)}
        metrics={{ ...METRICS, pattern_downwind_course_offset_deg: null }}
      />,
    );
    expect(container.querySelector(".pattern-downwind-fit")).toBeNull();
  });

  it("renders without leg boundaries (straight-in / older tracks)", () => {
    const { container } = render(<PatternTrack track={circuit(true)} />);
    expect(container.querySelectorAll("polyline").length).toBe(1);
    expect(container.querySelector(".pattern-leg-final")).not.toBeNull();
  });

  it("still renders when the track predates signed_distance_to_go", () => {
    const { container } = render(
      <PatternTrack track={circuit(false)} metrics={METRICS} />,
    );
    for (const line of container.querySelectorAll("polyline")) {
      expect(line.getAttribute("points")).not.toMatch(/NaN|Infinity/);
    }
  });

  it("says so instead of drawing an empty box when there is nothing to plot", () => {
    const empty = { ...circuit(true), samples: [] };
    const { container } = render(<PatternTrack track={empty} />);
    expect(container.querySelector("svg")).toBeNull();
  });
});
