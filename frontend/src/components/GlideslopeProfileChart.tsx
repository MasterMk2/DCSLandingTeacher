/**
 * Glideslope Profile Chart - Horizontal view showing ideal glideslope vs actual approach.
 * A4-friendly layout showing altitude vs distance-to-go with glideslope line.
 * Similar to an aircraft's profile view of the approach.
 */

import { useMemo } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
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
  centerline_dev_ft: number | null;  // Centerline deviation in feet
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

    const clDevFt = s.centerline_deviation !== null && s.centerline_deviation !== undefined
      ? mToFt(s.centerline_deviation)
      : null;

    return {
      distance_nm: distNm ?? 0,
      agl_ft: aglFt,
      ideal_ft: idealFt,
      glideslope_dev_ft: gsDevFt,
      centerline_dev_ft: clDevFt,
    };
  }).filter(p => p.distance_nm !== null && p.distance_nm >= 0)
    .sort((a, b) => b.distance_nm - a.distance_nm); // Far to near
}

export function GlideslopeProfileChart({ track }: GlideslopeProfileChartProps) {
  const points = useMemo(() => buildProfilePoints(track), [track]);

  if (points.length === 0) {
    return <p className="empty-message">グライドスローププロファイルデータがありません。</p>;
  }

  const maxDist = Math.max(...points.map(p => p.distance_nm), 2.5);
  const maxAgl = Math.max(
    ...points.filter(p => p.agl_ft !== null).map(p => p.agl_ft as number),
    maxDist * 6076.12 * Math.tan((track.glideslope_deg ?? 3.5) * Math.PI / 180) + 500
  );

  return (
    <div className="glideslope-profile-chart no-print">
      <h3>グライドスロープ プロファイル（横断面図）</h3>
      <div className="chart-legend">
        <span className="legend-item"><span className="legend-color ideal"></span>理想グライドスロープ ({(track.glideslope_deg ?? 3.5).toFixed(1)}°)</span>
        <span className="legend-item"><span className="legend-color actual"></span>実飛行AGL (ft)</span>
        <span className="legend-item"><span className="legend-color deviation"></span>偏差 (ft)</span>
      </div>
      <ResponsiveContainer width="100%" height={300}>
        <LineChart
          data={points}
          margin={{ top: 20, right: 30, bottom: 50, left: 60 }}
        >
          <CartesianGrid stroke="#22402f" strokeDasharray="3 3" />
          <XAxis
            dataKey="distance_nm"
            type="number"
            domain={[maxDist, 0]} // Reverse: far left (approach start) to right (touchdown)
            tick={{ fill: "#9fb8a8", fontSize: 11 }}
            label={{
              value: "着艦点までの距離 (nm)",
              position: "insideBottom",
              offset: -10,
              fill: "#9fb8a8",
              fontSize: 11,
            }}
            tickFormatter={(v) => v.toFixed(1)}
          />
          <YAxis
            tick={{ fill: "#9fb8a8", fontSize: 11 }}
            label={{
              value: "高度 / AGL (ft)",
              angle: -90,
              position: "insideLeft",
              offset: 15,
              fill: "#9fb8a8",
              fontSize: 11,
            }}
            domain={[0, Math.max(maxAgl, 2000)]}
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
              if (name === "centerline_dev_ft") return [`${Math.round(value)} ft`, "CL偏差"];
              return [value, name];
            }}
            labelFormatter={(v) => `${v.toFixed(1)} nm`}
          />
          <Legend wrapperStyle={{ fontSize: 11, marginTop: 8 }} />

          {/* Ideal glideslope line */}
          <Line
            name="理想グライドスロープ"
            dataKey="ideal_ft"
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

          {/* Centerline deviation */}
          <Line
            name="センターライン偏差"
            dataKey="centerline_dev_ft"
            stroke="#ff8fa3"
            strokeDasharray="2 2"
            dot={false}
            connectNulls
            strokeWidth={1.5}
            isAnimationActive={false}
            yAxisId="right"
          />

          {/* Touchdown marker at x=0 */}
          <line
            x1={0}
            y1={0}
            x2={0}
            y2={maxAgl}
            stroke="#ff4444"
            strokeWidth={1}
            strokeDasharray="4 4"
            className="touchdown-marker"
          />
        </LineChart>
      </ResponsiveContainer>
      <p className="chart-note">
        ※ 横軸は着艦点からの距離（左：進入開始側 → 右：着艦点）。グライドスロープ偏差とセンターライン偏差は右軸（ft）で表示。
      </p>
    </div>
  );
}