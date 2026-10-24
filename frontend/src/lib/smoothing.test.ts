import { describe, expect, it } from "vitest";
import { descentRateSmoothed, robustSmooth } from "./smoothing";

/** 0.2 s samples, the interval this server records at. */
const times = Array.from({ length: 60 }, (_, i) => i * 0.2);

describe("robustSmooth", () => {
  it("flattens the saw-tooth a two-point speed makes out of timestamp jitter", () => {
    // 70 m/s flown, read as 65 / 75 alternately (production #3523 did this at
    // ~10 m/s amplitude).
    const raw = times.map((_, i) => (i % 2 ? 75 : 65));
    const smooth = robustSmooth(times, raw, 1.0);
    // +-5 m/s of saw-tooth leaves under 1 m/s (~2 kt) of ripple.
    // Away from the ends (three windows deep, where the windows are lopsided).
    for (const v of smooth.slice(15, -15)) expect(Math.abs((v as number) - 70)).toBeLessThan(1);
  });

  it("drops a single jumped value instead of smearing it into its neighbours", () => {
    const raw = times.map((_, i) => (i === 30 ? 106 : 70));
    const smooth = robustSmooth(times, raw, 1.0);
    expect(smooth[30]).toBeCloseTo(70, 5);
    expect(smooth[29]).toBeCloseTo(70, 5);
  });

  it("follows a real deceleration", () => {
    const raw = times.map((t) => 90 - 2 * t); // 2 m/s^2
    const smooth = robustSmooth(times, raw, 1.0);
    expect(smooth[30] as number).toBeCloseTo(raw[30], 1);
  });

  it("keeps gaps as gaps", () => {
    const raw = times.map((_, i) => (i === 10 ? null : 70));
    expect(robustSmooth(times, raw, 1.0)[10]).toBeNull();
  });
});

describe("descentRateSmoothed", () => {
  it("reads a steady descent through a jumped height", () => {
    // 3.5 m/s down; one sample 40 m off, which a neighbour difference would
    // turn into a 200 m/s (~39,000 fpm) spike.
    const heights = times.map((t, i) => 300 - 3.5 * t + (i === 25 ? 40 : 0));
    const rates = descentRateSmoothed(times, heights, 1.0);
    for (const r of rates.slice(5, -5)) expect(r as number).toBeCloseTo(3.5, 5);
  });

  it("widens the window for a sparse record instead of drawing nothing", () => {
    // 2 s samples: +-1 s holds one point, +-2 s three.
    const sparse = [0, 2, 4, 6, 8];
    const rates = descentRateSmoothed(sparse, sparse.map((t) => 100 - 3 * t), 1.0);
    expect(rates.slice(1, -1)).toEqual([3, 3, 3]);
  });

  it("needs three heights even in the widest window", () => {
    expect(descentRateSmoothed([0, 0.2], [10, 9], 1.0)).toEqual([null, null]);
  });
});
