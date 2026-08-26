/**
 * @vitest-environment jsdom
 *
 * Recharts validates its axis wiring while mounting, and an inconsistency
 * throws rather than degrading: a `yAxisId` on a <Line> with no matching
 * <YAxis> raises "Invariant failed", which unmounts the whole React tree and
 * leaves the detail page blank. That shipped once already, so the chart is
 * mounted here for real.
 *
 * ResponsiveContainer has to be replaced: it measures its parent, and jsdom
 * reports 0x0, so Recharts would skip rendering the chart entirely and the
 * test would pass no matter how badly the axes were wired.
 */
import { cloneElement, isValidElement } from "react";
import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("recharts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("recharts")>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactElement }) =>
      isValidElement(children)
        ? cloneElement(children, { width: 800, height: 300 } as never)
        : children,
  };
});

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
