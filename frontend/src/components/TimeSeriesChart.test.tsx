/**
 * @vitest-environment jsdom
 *
 * Mounted for real for the same reason GlideslopeProfileChart is: a <Line>
 * whose `yAxisId` has no matching <YAxis> makes Recharts throw at mount
 * and blank the whole detail page. The load-factor chart is the first one
 * here with two Y axes, one of them conditional.
 */
import { cloneElement, type ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";

// jsdom has no ResizeObserver and no layout, so a ResponsiveContainer never
// measures anything and mounts nothing -- which would make every assertion
// below pass vacuously. Give the charts a fixed size instead, the way the
// print-sheet chart already draws itself.
vi.mock("recharts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("recharts")>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactElement }) => (
      <div>{cloneElement(children, { width: 600, height: 200 })}</div>
    ),
  };
});

const { TimeSeriesChart, buildRows, hasLoadFactor } = await import("./TimeSeriesChart");

function track(overrides: Record<string, unknown> = {}) {
  return {
    kind: "land",
    outcome: "full_stop",
    glideslope_deg: 3.0,
    course_deg: 90,
    touchdown_time: 200,
    samples: Array.from({ length: 40 }, (_, i) => ({
      time: 120 + i * 2,
      distance_to_go: 4000 - i * 100,
      signed_distance_to_go: 4000 - i * 100,
      glideslope_deviation: 3 - i * 0.1,
      centerline_deviation: 50 - i,
      speed: 80,
      aoa: null,
      agl: 200 - i * 5,
      load_factor: i >= 5 && i <= 15 ? 2.4 : 1.0,
      turn_rate_deg_s: i >= 5 && i <= 15 ? 9.1 : 0.2,
      roll: i >= 5 && i <= 15 ? -64 : -2,
      pitch: 3,
    })),
    ...overrides,
  };
}

describe("TimeSeriesChart", () => {
  it("mounts the load-factor chart with both of its axes", () => {
    const { container } = render(
      <TimeSeriesChart
        track={track() as never}
        metrics={{ pattern_break_start_time: 130, pattern_break_end_time: 150 }}
      />,
    );
    expect(container.textContent).toContain("荷重倍数（G）");
    // Four charts: the G chart carries a left (G) and a right (bank) axis.
    expect(container.querySelectorAll(".recharts-yAxis").length).toBe(5);
    expect(container.querySelectorAll(".recharts-line").length).toBe(7);
  });

  it("leaves the load-factor chart out when the track has no G series", () => {
    const bare = track({
      samples: (track().samples as Array<Record<string, unknown>>).map((s) => ({
        ...s,
        load_factor: null,
        roll: null,
      })),
    });
    const { container } = render(<TimeSeriesChart track={bare as never} />);
    expect(container.textContent).not.toContain("荷重倍数");
    expect(container.querySelectorAll(".recharts-yAxis").length).toBe(3);
  });

  it("draws the bank axis only when the recording carries attitude", () => {
    const noRoll = track({
      samples: (track().samples as Array<Record<string, unknown>>).map((s) => ({
        ...s,
        roll: null,
      })),
    });
    const { container } = render(<TimeSeriesChart track={noRoll as never} />);
    expect(container.textContent).toContain("荷重倍数（G）");
    expect(container.textContent).not.toContain("バンク角");
    expect(container.querySelectorAll(".recharts-yAxis").length).toBe(4);
  });
});

describe("buildRows", () => {
  it("copies the G into the break series only inside the break leg", () => {
    const rows = buildRows(track() as never, {
      pattern_break_start_time: 130,
      pattern_break_end_time: 150,
    });
    const inside = rows.filter((r) => r.gBreak !== null);
    expect(inside.length).toBe(11);
    expect(inside.every((r) => r.g === r.gBreak)).toBe(true);
    expect(rows.find((r) => r.t === 120 - 200)?.gBreak).toBeNull();
    expect(hasLoadFactor(rows)).toBe(true);
  });

  it("treats a corrupt roll as no bank rather than a 13000 degree one", () => {
    const rows = buildRows(
      track({
        samples: [
          { time: 100, distance_to_go: 100, load_factor: 1.0, roll: 13605 },
          { time: 101, distance_to_go: 90, load_factor: 1.0, roll: -61.5 },
        ],
      }) as never,
    );
    expect(rows[0].bank).toBeNull();
    expect(rows[1].bank).toBe(62);
  });
});
