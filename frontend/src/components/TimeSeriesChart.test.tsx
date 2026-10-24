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

const { TimeSeriesChart, buildRows, hasLoadFactor, timeTicks } = await import("./TimeSeriesChart");

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

  it("adds an AOA chart with the on-speed band when the track has an AOA series", () => {
    const withAoa = track({
      samples: (track().samples as Array<Record<string, unknown>>).map((s) => ({
        ...s,
        aoa_est: 8.0,
      })),
    });
    const { container } = render(
      <TimeSeriesChart
        track={withAoa as never}
        metrics={{ aoa_on_speed_min_deg: 7.4, aoa_on_speed_max_deg: 8.8 }}
      />,
    );
    expect(container.textContent).toContain("網掛け = オンスピード 7.4–8.8°");
    expect(container.querySelectorAll(".recharts-reference-area").length).toBe(1);
    expect(container.querySelectorAll(".recharts-yAxis").length).toBe(6);
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

  it("smooths the speed and the descent rate instead of plotting sample-to-sample noise", () => {
    // 0.2 s samples, speed alternating 65/75 m/s around 70 and a steady
    // 3 m/s descent with one jumped height.
    const samples = Array.from({ length: 50 }, (_, i) => ({
      time: 100 + i * 0.2,
      distance_to_go: 1000 - i * 14,
      speed: i % 2 ? 75 : 65,
      agl: 100 - 3 * i * 0.2 + (i === 25 ? 30 : 0),
    }));
    const rows = buildRows(track({ touchdown_time: 110, samples }) as never);
    for (const r of rows.slice(5, -5)) {
      expect(Math.abs((r.kt as number) - 136)).toBeLessThanOrEqual(3); // 70 m/s; raw is +-10 kt
      expect(Math.abs((r.fpm as number) - 591)).toBeLessThanOrEqual(1); // 3 m/s
    }
  });

  it("does not smooth across the touchdown", () => {
    // A trap: 65 m/s up to touchdown at t=110, then stopped within a second.
    const samples = Array.from({ length: 50 }, (_, i) => {
      const time = 105 + i * 0.2;
      return {
        time,
        distance_to_go: 500 - i * 10,
        speed: time < 110 ? 65 : Math.max(0, 65 - 65 * (time - 110)),
        agl: time < 110 ? 15 - 3.5 * (time - 105) : -2.5,
      };
    });
    const rows = buildRows(track({ touchdown_time: 110, samples }) as never);
    const before = rows.filter((r) => r.t < 0 && r.t >= -2);
    // Still flying at 65 m/s (126 kt) and sinking 3.5 m/s (689 fpm) right up
    // to the wires, not decelerating or levelling off ahead of them.
    for (const r of before) {
      expect(r.kt).toBe(126);
      expect(Math.abs((r.fpm as number) - 689)).toBeLessThanOrEqual(1);
    }
  });

  it("puts round-second ticks across the record", () => {
    const rows = [{ t: -109.4 }, { t: -40 }, { t: 5.2 }] as never;
    expect(timeTicks(rows)).toEqual([-100, -90, -80, -70, -60, -50, -40, -30, -20, -10, 0]);
    expect(timeTicks([{ t: -6.4 }, { t: 0.2 }] as never)).toEqual([-6, -5, -4, -3, -2, -1, 0]);
  });

  it("plots the recorded AOA when there is one and the estimate otherwise, never a mix", () => {
    const estimated = buildRows(
      track({
        samples: [
          { time: 100, distance_to_go: 100, aoa: null, aoa_est: 8.04 },
          { time: 101, distance_to_go: 90, aoa: null, aoa_est: 7.96 },
        ],
      }) as never,
    );
    expect(estimated.map((r) => r.aoa)).toEqual([8.0, 8.0]);
    const recorded = buildRows(
      track({
        samples: [
          { time: 100, distance_to_go: 100, aoa: 6.5, aoa_est: 8.0 },
          { time: 101, distance_to_go: 90, aoa: null, aoa_est: 8.0 },
        ],
      }) as never,
    );
    expect(recorded.map((r) => r.aoa)).toEqual([6.5, null]);
  });
});
