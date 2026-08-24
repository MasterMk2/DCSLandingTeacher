/** Time-series charts: deviations, speed and descent rate (Recharts). */

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
import { descentRateSeries } from "../lib/gcaGeometry";
import { mToFt, msToFpm, msToKnots } from "../lib/format";
import type { ApproachTrack } from "../types/api";

export interface TimeSeriesChartProps {
  track: ApproachTrack;
}

interface Row {
  t: number; // seconds before touchdown (negative)
  gs: number | null;
  cl: number | null;
  kt: number | null;
  fpm: number | null;
}

function buildRows(track: ApproachTrack): Row[] {
  const tdTime = track.touchdown_time ?? track.samples.at(-1)?.time ?? 0;
  const rates = new Map(descentRateSeries(track.samples).map((r) => [r.time, r.rateMs]));
  return track.samples.map((s) => ({
    t: Math.round((s.time - tdTime) * 10) / 10,
    // Issue D-4: display deviations in feet (backend keeps meters).
    gs: s.glideslope_deviation !== null && s.glideslope_deviation !== undefined
      ? Math.round(mToFt(s.glideslope_deviation))
      : null,
    cl: s.centerline_deviation !== null && s.centerline_deviation !== undefined
      ? Math.round(mToFt(s.centerline_deviation))
      : null,
    kt: s.speed !== null && s.speed !== undefined ? Math.round(msToKnots(s.speed)) : null,
    fpm:
      rates.has(s.time) && rates.get(s.time) !== undefined
        ? Math.round(msToFpm(rates.get(s.time) as number))
        : null,
  }));
}

const AXIS_STYLE = { fill: "#9fb8a8", fontSize: 11 };
const TOOLTIP_STYLE = {
  backgroundColor: "#10241a",
  border: "1px solid #2f5c44",
  color: "#d7efe0",
};

export function TimeSeriesChart({ track }: TimeSeriesChartProps) {
  const rows = useMemo(() => buildRows(track), [track]);

  if (rows.length === 0) {
    return <p className="empty-message">時系列データがありません。</p>;
  }

  return (
    <div className="timeseries">
      <section>
        <h3>偏差（グライドスロープ / センターライン）</h3>
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke="#22402f" strokeDasharray="3 3" />
            <XAxis
              dataKey="t"
              tick={AXIS_STYLE}
              label={{ value: "接地前の時間 (秒)", position: "insideBottom", offset: -2, fill: "#9fb8a8", fontSize: 11 }}
            />
            <YAxis tick={AXIS_STYLE} unit="ft" width={56} />
            <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v) => `${v} ft`} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line name="GS 偏差" dataKey="gs" stroke="#39d98a" dot={false} connectNulls />
            <Line name="CL 偏差" dataKey="cl" stroke="#ffd166" dot={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </section>

      <section>
        <h3>速度</h3>
        <ResponsiveContainer width="100%" height={180}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke="#22402f" strokeDasharray="3 3" />
            <XAxis dataKey="t" tick={AXIS_STYLE} />
            <YAxis tick={AXIS_STYLE} unit="kt" width={56} />
            <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v) => `${v} kt`} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line name="対気速度" dataKey="kt" stroke="#6ab7ff" dot={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </section>

      <section>
        <h3>降下率（AGL 差分から算出）</h3>
        <ResponsiveContainer width="100%" height={180}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke="#22402f" strokeDasharray="3 3" />
            <XAxis dataKey="t" tick={AXIS_STYLE} />
            <YAxis tick={AXIS_STYLE} unit="fpm" width={64} />
            <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v) => `${v} fpm`} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line name="降下率" dataKey="fpm" stroke="#ff8fa3" dot={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </section>
    </div>
  );
}
