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
