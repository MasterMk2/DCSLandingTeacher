/**
 * Glideslope Profile Chart - Horizontal view showing ideal glideslope vs actual approach.
 * A4-friendly layout showing altitude vs distance-to-go with glideslope line.
 * Similar to an aircraft's profile view of the approach.
 */

import { useMemo } from "react";
import {
  CartesianGrid,
  Line,
  ReferenceLine,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { mToFt, mToNm } from "../lib/format";
import type { ApproachTrack } from "../types/api";

export interface GlideslopeProfileChartProps {
  track: ApproachTrack;
}

interface ProfilePoint {
  distance_nm: number;       // Distance to touchdown in NM
  agl_ft: number | null;     // Actual AGL in feet (height above deck)
  ideal_ft: number | null;   // Ideal glideslope altitude in feet
  glideslope_dev_ft: number | null;  // Glideslope deviation in feet
}

/** Round an axis maximum up to a readable step, so ticks land on round
 *  numbers instead of values like 1634 / 897 / -3. */
function niceCeil(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    const candidate = step * magnitude;
    if (value <= candidate) return candidate;
  }
  return 10 * magnitude;
}

/** Evenly spaced ticks. Recharts otherwise derives them from the data range
 *  and only appends the domain maximum, which produced axes like
 *  -3 / 547 / 1097 / 2000. */
function evenTicks(min: number, max: number, count = 5): number[] {
  return Array.from({ length: count }, (_, i) => min + ((max - min) * i) / (count - 1));
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
  }).filter(p => p.distance_nm !== null && p.distance_nm >= 0)
    .sort((a, b) => b.distance_nm - a.distance_nm); // Far to near
}

export function GlideslopeProfileChart({ track }: GlideslopeProfileChartProps) {
  const points = useMemo(() => buildProfilePoints(track), [track]);

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
  const aglAxisMax = niceCeil(maxAgl);
  const distAxisMax = niceCeil(maxDist);
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
  const devAxisMax = niceCeil(maxDev);

  return (
    <div className="glideslope-profile-chart no-print">
      <h3>グライドスロープ プロファイル（横断面図）</h3>
      <div className="chart-legend">
        <span className="legend-item"><span className="legend-color ideal"></span>理想グライドスロープ ({(track.glideslope_deg ?? 3.5).toFixed(1)}°)</span>
        <span className="legend-item"><span className="legend-color actual"></span>実飛行AGL (ft)</span>
        <span className="legend-item"><span className="legend-color deviation"></span>グライドスロープ偏差 (ft, 右軸)</span>
      </div>
      <ResponsiveContainer width="100%" height={300}>
        <LineChart
          data={points}
          margin={{ top: 8, right: 70, bottom: 28, left: 60 }}
        >
          <CartesianGrid stroke="#22402f" strokeDasharray="3 3" />
          <XAxis
            dataKey="distance_nm"
            type="number"
            domain={[0, distAxisMax]}
            ticks={evenTicks(0, distAxisMax)}
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
            tickFormatter={(v) => v.toFixed(distAxisMax < 0.5 ? 2 : 1)}
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
            domain={[0, aglAxisMax]}
            ticks={evenTicks(0, aglAxisMax)}
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
            domain={[-devAxisMax, devAxisMax]}
            ticks={evenTicks(-devAxisMax, devAxisMax)}
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
        </LineChart>
      </ResponsiveContainer>
      <p className="chart-note">
        ※ 横軸は接地点までの距離（左：進入開始側 → 右：接地点）。グライドスロープ偏差は右軸（ft、+ が理想より上）。横ずれは方位スコープを参照。
      </p>
    </div>
  );
}