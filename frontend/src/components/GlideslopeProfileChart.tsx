/**
 * Glideslope Profile Chart - Horizontal view showing ideal glideslope vs actual approach.
 * A4-friendly layout showing altitude vs distance-to-go with glideslope line.
 * Similar to an aircraft's profile view of the approach.
 */

import { useEffect, useMemo, useRef } from "react";
import {
  CartesianGrid,
  Line,
  ReferenceLine,
  LineChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { mToFt, mToNm } from "../lib/format";
import type { ApproachTrack } from "../types/api";

/** Drawn at a fixed size and scaled by CSS: see the note at the chart. */
const CHART_WIDTH = 760;
const CHART_HEIGHT = 320;

export interface GlideslopeProfileChartProps {
  track: ApproachTrack;
  /** Grading metrics; carries the roll-out time used for the base->final mark. */
  metrics?: Record<string, unknown> | null;
}

/** Distance-to-go (nm) at the roll-out onto final, or null if unknown.
 *
 * This is the point the approach has to be ON the glidepath by: everything
 * left of it on this chart is the turn, where being off the slope is
 * expected and is not what the grade is about. Without the mark the reader
 * has no way to tell which part of the trace they are supposed to judge.
 */
export function rolloutDistanceNm(
  track: ApproachTrack,
  metrics?: Record<string, unknown> | null,
): number | null {
  const num = (v: unknown) =>
    typeof v === "number" && Number.isFinite(v) ? v : null;
  const touchdown = num(track.touchdown_time);
  let time = num(metrics?.["pattern_rollout_time"]);
  if (time === null && touchdown !== null) {
    // Straight-in: no turn to roll out of, so fall back to where the graded
    // final began (the stabilization gate).
    const window = num(metrics?.["final_window_s"]);
    if (window !== null) time = touchdown - window;
  }
  if (time === null) return null;

  let best: number | null = null;
  let bestGap = Infinity;
  for (const sample of track.samples) {
    const gap = Math.abs(sample.time - time);
    if (gap < bestGap && Number.isFinite(sample.distance_to_go)) {
      bestGap = gap;
      best = sample.distance_to_go;
    }
  }
  // A match many seconds away means the sample simply is not in the record.
  if (best === null || bestGap > 3.0) return null;
  return mToNm(best);
}

interface ProfilePoint {
  distance_nm: number;       // Distance to touchdown in NM
  agl_ft: number | null;     // Actual AGL in feet (height above deck)
  ideal_ft: number | null;   // Ideal glideslope altitude in feet
  glideslope_dev_ft: number | null;  // Glideslope deviation in feet
}

/** An axis whose *step* is a round number, not just its maximum.
 *  Rounding only the maximum and then splitting it evenly gives ticks like
 *  0.6 / 1.3 / 1.9 (2.5 divided into four); choosing the step first keeps
 *  them readable whatever the range. */
function niceAxis(maxValue: number, intervals = 4): { max: number; ticks: number[] } {
  const safe = Number.isFinite(maxValue) && maxValue > 0 ? maxValue : 1;
  const rough = safe / intervals;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const step =
    [1, 2, 2.5, 5, 10].find((m) => rough <= m * magnitude) ?? 10;
  const stepSize = (typeof step === "number" ? step : 10) * magnitude;
  const count = Math.ceil(safe / stepSize);
  const decimals = Math.max(0, -Math.floor(Math.log10(stepSize)));
  const round = (v: number) => Number(v.toFixed(decimals + 2));
  return {
    max: round(count * stepSize),
    ticks: Array.from({ length: count + 1 }, (_, i) => round(i * stepSize)),
  };
}

function buildProfilePoints(track: ApproachTrack): ProfilePoint[] {
  const glideslopeDeg = track.glideslope_deg ?? 3.5;
  const tanSlope = Math.tan(glideslopeDeg * Math.PI / 180);

  return track.samples.map((s) => {
    const distNm = s.distance_to_go !== null && s.distance_to_go !== undefined
      ? mToNm(s.distance_to_go)
      : null;

    // Use AGL (height above deck) as the vertical coordinate
    const aglFt = s.agl !== null && s.agl !== undefined
      ? mToFt(s.agl)
      : null;

    // Ideal AGL based on glideslope (0 at touchdown)
    const idealFt = distNm !== null ? distNm * 6076.12 * tanSlope : null;

    const gsDevFt = s.glideslope_deviation !== null && s.glideslope_deviation !== undefined
      ? mToFt(s.glideslope_deviation)
      : null;

    return {
      distance_nm: distNm ?? 0,
      agl_ft: aglFt,
      ideal_ft: idealFt,
      glideslope_dev_ft: gsDevFt,
    };
  }).filter(p => p.distance_nm !== null && p.distance_nm >= 0);
}

/** Keep only the final inbound leg (last distance maximum -> touchdown).
 *
 *  Distance-to-go is not monotonic over a captured approach: in an overhead
 *  break the aircraft is still flying *away* from the touchdown point while
 *  it turns, so the stored track rises to a peak before coming back in.
 *  Plotting all of it against distance folds the outbound half back over the
 *  inbound half and the line crosses itself -- the same x holds two different
 *  altitudes. Sorting by distance (as this used to) only scrambles it
 *  further, since that discards time order entirely.
 */
function inboundLeg(points: ProfilePoint[]): ProfilePoint[] {
  if (points.length < 2) return points;
  // Walk back from touchdown while distance keeps growing; where it starts
  // shrinking again we have reached the turn, and everything before that
  // belongs to the outbound part of the pattern.
  let start = points.length - 1;
  for (let i = points.length - 1; i > 0; i--) {
    if (points[i - 1].distance_nm < points[i].distance_nm) break;
    start = i - 1;
  }
  return points.slice(start);
}

export function GlideslopeProfileChart({ track, metrics }: GlideslopeProfileChartProps) {
  const points = useMemo(() => inboundLeg(buildProfilePoints(track)), [track]);
  const rolloutNm = useMemo(
    () => rolloutDistanceNm(track, metrics),
    [track, metrics],
  );
  // On a phone the chart is wider than the screen and scrolls. Start it at
  // the touchdown end: the x axis is reversed, so the default left edge is
  // the far end of the approach -- the least interesting part of it.
  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const box = scroller.current;
    if (box) box.scrollLeft = box.scrollWidth;
  }, [track]);

  if (points.length === 0) {
    return <p className="empty-message">グライドスローププロファイルデータがありません。</p>;
  }

  // Fit the axes to the approach that was actually flown. Fixed floors (the
  // 2.5 nm / 2000 ft this used to force) squeeze a short approach -- a
  // helicopter, or a recording that only caught short final -- into a sliver
  // at the edge of an otherwise empty plot.
  const maxDist = Math.max(...points.map(p => p.distance_nm), 0.2);
  const idealAtStart =
    maxDist * 6076.12 * Math.tan((track.glideslope_deg ?? 3.5) * Math.PI / 180);
  const maxAgl =
    Math.max(
      ...points.filter(p => p.agl_ft !== null).map(p => p.agl_ft as number),
      idealAtStart,
      50
    ) * 1.15;
  const aglAxis = niceAxis(maxAgl);
  const distAxis = niceAxis(maxDist);
  // Symmetric so the zero line (= on slope) stays centred. Only the
  // glideslope deviation belongs here: this is a vertical cross-section, and
  // mixing in the lateral offset both misreads as a height and stretches the
  // axis by its own magnitude (hundreds of feet on a turning approach),
  // which flattens the deviation this chart exists to show. Lateral error is
  // the azimuth scope's job.
  const maxDev = Math.max(
    100,
    ...points
      .map(p => p.glideslope_dev_ft)
      .filter((v): v is number => v !== null && Number.isFinite(v))
      .map(Math.abs)
  );
  const devHalf = niceAxis(maxDev, 2);
  // Mirror the positive half so zero (= on slope) stays centred.
  const devTicks = [
    ...devHalf.ticks.filter((t) => t > 0).map((t) => -t).reverse(),
    ...devHalf.ticks,
  ];

  return (
    <div className="glideslope-profile-chart">
      <h3>グライドスロープ プロファイル（横断面図）</h3>
      <div className="chart-legend">
        <span className="legend-item"><span className="legend-color ideal"></span>理想グライドスロープ ({(track.glideslope_deg ?? 3.5).toFixed(1)}°)</span>
        <span className="legend-item"><span className="legend-color actual"></span>実飛行AGL (ft)</span>
        <span className="legend-item"><span className="legend-color deviation"></span>グライドスロープ偏差 (ft, 右軸)</span>
        {rolloutNm !== null && (
          <span className="legend-item"><span className="legend-color rollout"></span>ベース→ファイナル（ここでグライドスロープに乗る）</span>
        )}
      </div>
      {/* Fixed size, scaled by CSS, rather than a ResponsiveContainer.
          The container measures its parent on mount and does not re-measure
          for print, so the printed chart came out drawn at the on-screen
          column width and squeezed into a third of the page. */}
      <div className="chart-scroll" ref={scroller}>
      <LineChart
          width={CHART_WIDTH}
          height={CHART_HEIGHT}
          data={points}
          margin={{ top: 8, right: 70, bottom: 28, left: 60 }}
        >
          <CartesianGrid stroke="#22402f" strokeDasharray="3 3" />
          <XAxis
            dataKey="distance_nm"
            type="number"
            domain={[0, distAxis.max]}
            ticks={distAxis.ticks}
            // `reversed`, not a descending domain: Recharts ignores the
            // latter, which had the approach running touchdown-first and
            // contradicting this chart's own caption.
            reversed
            tick={{ fill: "#9fb8a8", fontSize: 11 }}
            label={{
              value: "接地点までの距離 (nm)",
              position: "insideBottom",
              offset: -10,
              fill: "#9fb8a8",
              fontSize: 11,
            }}
            tickFormatter={(v) => v.toFixed(distAxis.max < 0.5 ? 2 : 1)}
          />
          <YAxis
            yAxisId="left"
            tick={{ fill: "#9fb8a8", fontSize: 11 }}
            label={{
              value: "高度 / AGL (ft)",
              angle: -90,
              position: "insideLeft",
              offset: 15,
              fill: "#9fb8a8",
              fontSize: 11,
            }}
            domain={[0, aglAxis.max]}
            ticks={aglAxis.ticks}
            tickFormatter={(v) => `${Math.round(v)}`}
          />
          {/* Deviations are signed and an order of magnitude smaller than the
              altitudes, so they need their own axis; without it Recharts
              throws on the yAxisId="right" the deviation lines declare. */}
          <YAxis
            yAxisId="right"
            orientation="right"
            tick={{ fill: "#9fb8a8", fontSize: 11 }}
            label={{
              value: "GS偏差 (ft)",
              angle: 90,
              position: "insideRight",
              offset: 15,
              fill: "#9fb8a8",
              fontSize: 11,
            }}
            domain={[-devHalf.max, devHalf.max]}
            ticks={devTicks}
            tickFormatter={(v) => `${Math.round(v)}`}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "#10241a",
              border: "1px solid #2f5c44",
              color: "#d7efe0",
            }}
            formatter={(value: number, name: string) => {
              if (name === "agl_ft" || name === "ideal_ft") return [`${Math.round(value)} ft`, name];
              if (name === "glideslope_dev_ft") return [`${Math.round(value)} ft`, "GS偏差"];
              return [value, name];
            }}
            labelFormatter={(v) => `${v.toFixed(1)} nm`}
          />
          {/* Ideal glideslope line */}
          <Line
            name="理想グライドスロープ"
            dataKey="ideal_ft"
            yAxisId="left"
            stroke="#39d98a"
            strokeDasharray="6 4"
            dot={false}
            connectNulls
            strokeWidth={2}
            isAnimationActive={false}
          />

          {/* Actual AGL */}
          <Line
            name="実飛行AGL"
            dataKey="agl_ft"
            yAxisId="left"
            stroke="#6ab7ff"
            dot={false}
            connectNulls
            strokeWidth={2}
            isAnimationActive={false}
          />

          {/* Glideslope deviation */}
          <Line
            name="グライドスロープ偏差"
            dataKey="glideslope_dev_ft"
            stroke="#ffd166"
            strokeDasharray="3 3"
            dot={false}
            connectNulls
            strokeWidth={1.5}
            isAnimationActive={false}
            yAxisId="right"
          />

          {/* Touchdown, in data space (a raw <line> would be placed in SVG
              coordinates and sit at the edge of the plot instead). */}
          <ReferenceLine
            x={0}
            yAxisId="left"
            stroke="#ff4444"
            strokeDasharray="4 4"
          />

          {/* Base -> final. Left of this the aircraft is still turning and
              being off the slope is expected; right of it is what is graded. */}
          {rolloutNm !== null && (
            <ReferenceLine
              x={rolloutNm}
              yAxisId="left"
              stroke="#b98bff"
              strokeDasharray="6 4"
              label={{
                value: "ベース→ファイナル",
                position: "insideTopLeft",
                fill: "#b98bff",
                fontSize: 10,
              }}
            />
          )}
        </LineChart>
      </div>
      <p className="chart-note">
        ※ 横軸は接地点までの距離（左：進入開始側 → 右：接地点）。グライドスロープ偏差は右軸（ft、+ が理想より上）。横ずれはパターン軌跡を参照。
        紫の破線がベース→ファイナルの境目で、採点対象はその右側。
      </p>
    </div>
  );
}