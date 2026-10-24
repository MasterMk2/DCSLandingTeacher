/**
 * Smoothing for the time-series charts.
 *
 * The recorded speed is a two-point ground speed over one sample interval
 * (0.13-0.27 s on this server's recordings), and the old descent-rate line
 * was the height difference between neighbouring samples. Over an interval
 * that short, a few tens of milliseconds of timestamp jitter or one jumped
 * position is a large fraction of the motion: production tracks showed
 * speed alternating by 10 m/s (~20 kt) sample to sample, spikes of 20-36 m/s,
 * and descent-rate spikes of thousands of fpm -- a saw-tooth that hid the
 * trend the chart is there to show. An aircraft cannot change speed or sink
 * rate that fast, so a window of about two seconds loses nothing real.
 *
 * Both smoothers are robust (median based), so a jumped position drops out
 * instead of smearing into its neighbours. Display only: grading does its
 * own derivation (backend app.grading.kinematics).
 */

/** Median of a non-empty array (does not modify it). */
function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/** First index whose time is at or after `value` (`times` ascending). */
function firstAtOrAfter(times: number[], value: number): number {
  let lo = 0;
  let hi = times.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] < value) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** Index range [lo, hi] of the samples within `halfWindowS` of `times[i]`.
 *  `lo` is a starting guess no greater than the answer (the previous
 *  window's start, when sliding forward). */
function windowAround(times: number[], i: number, halfWindowS: number, lo: number): [number, number] {
  while (times[lo] < times[i] - halfWindowS) lo++;
  let hi = i;
  while (hi + 1 < times.length && times[hi + 1] <= times[i] + halfWindowS) hi++;
  return [lo, hi];
}

/** Apply `reduce` to each +-`halfWindowS` window of `v` (times `t`). */
function slide(t: number[], v: number[], halfWindowS: number, reduce: (w: number[]) => number): number[] {
  const out: number[] = [];
  let lo = 0;
  for (let k = 0; k < v.length; k++) {
    let hi;
    [lo, hi] = windowAround(t, k, halfWindowS, lo);
    out.push(reduce(v.slice(lo, hi + 1)));
  }
  return out;
}

/**
 * Moving median, then a moving mean twice, each over +-`halfWindowS`
 * seconds.
 *
 * The median removes a jumped value outright; the means then even out what
 * the median leaves of a regular saw-tooth (on a window holding six low and
 * five high values the median itself flips from one to the other, and the
 * window's sample count wobbles by one with the timestamps). Measured on a
 * +-5 m/s saw-tooth at 0.2 s: one mean leaves up to 1.8 m/s of ripple, two
 * leave 0.9. All three keep a straight-line trend -- a steady deceleration
 * -- as it is. A one-pass "reject beyond k x MAD, average the rest" does
 * not work: on a two-valued saw-tooth the MAD is zero and half the samples
 * are thrown away. `times` must be ascending; `null` values are skipped and
 * stay `null`.
 */
export function robustSmooth(
  times: number[],
  values: (number | null)[],
  halfWindowS: number,
): (number | null)[] {
  const idx: number[] = [];
  for (let i = 0; i < values.length; i++) if (values[i] !== null) idx.push(i);
  const t = idx.map((i) => times[i]);
  const mean = (w: number[]) => w.reduce((s, x) => s + x, 0) / w.length;
  const medians = slide(t, idx.map((i) => values[i] as number), halfWindowS, median);
  const means = slide(t, slide(t, medians, halfWindowS, mean), halfWindowS, mean);
  const out: (number | null)[] = values.map(() => null);
  idx.forEach((i, k) => {
    out[i] = means[k];
  });
  return out;
}

/**
 * Descent rate (m/s, down positive) at each sample: the Theil-Sen slope --
 * the median of the slopes between every pair of points -- of height
 * against time over +-`halfWindowS` seconds. Robust to up to ~29% bad
 * points in a window, which a least-squares slope is not.
 *
 * A sparse record (1 Hz exports, older tracks) has fewer than three heights
 * in that window; it is then widened, doubling, up to `maxHalfWindowS` --
 * the same rule as the backend's kinematics fit. `null` where the sample
 * has no height or even the widest window holds fewer than three.
 */
export function descentRateSmoothed(
  times: number[],
  heights: (number | null)[],
  halfWindowS: number,
  maxHalfWindowS = 3 * halfWindowS,
): (number | null)[] {
  const idx: number[] = [];
  for (let i = 0; i < heights.length; i++) if (heights[i] !== null) idx.push(i);
  const t = idx.map((i) => times[i]);
  const h = idx.map((i) => heights[i] as number);
  const out: (number | null)[] = heights.map(() => null);
  for (let k = 0; k < idx.length; k++) {
    let half = halfWindowS;
    let [lo, hi] = windowAround(t, k, half, firstAtOrAfter(t, t[k] - half));
    while (hi - lo < 2 && half < maxHalfWindowS) {
      half = Math.min(maxHalfWindowS, half * 2);
      [lo, hi] = windowAround(t, k, half, firstAtOrAfter(t, t[k] - half));
    }
    if (hi - lo < 2) continue;
    const slopes: number[] = [];
    for (let a = lo; a < hi; a++) {
      for (let b = a + 1; b <= hi; b++) {
        const dt = t[b] - t[a];
        if (dt > 0) slopes.push((h[a] - h[b]) / dt);
      }
    }
    if (slopes.length) out[idx[k]] = median(slopes);
  }
  return out;
}
