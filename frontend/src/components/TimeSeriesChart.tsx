/** Time-series charts: deviations, speed, descent rate and load factor (Recharts). */

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
  /** Grading metrics; the break leg's boundaries live here as mission
   *  times and pick out the G actually pulled in the break. */
  metrics?: Record<string, unknown> | null;
}

interface Row {
  t: number; // seconds before touchdown (negative)
  gs: number | null;
  cl: number | null;
  kt: number | null;
  fpm: number | null;
  /** Normal load factor, G. */
  g: number | null;
  /** The same value, only inside the break leg, so the break is drawn on
   *  top of the whole series in its own colour. Recharts' category axis
   *  cannot shade a time span, so the highlight is a second series. */
  gBreak: number | null;
  /** |Roll|, degrees, when the recording carries attitude. */
  bank: number | null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function buildRows(
  track: ApproachTrack,
  metrics?: Record<string, unknown> | null,
): Row[] {
  const tdTime = track.touchdown_time ?? track.samples.at(-1)?.time ?? 0;
  const rates = new Map(descentRateSeries(track.samples).map((r) => [r.time, r.rateMs]));
  const breakStart = num(metrics?.["pattern_break_start_time"]);
  const breakEnd = num(metrics?.["pattern_break_end_time"]);
  return track.samples.map((s) => {
    const g = num(s.load_factor);
    const inBreak =
      breakStart !== null && breakEnd !== null && s.time >= breakStart && s.time <= breakEnd;
    const roll = num(s.roll);
    return {
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
      g: g !== null ? Math.round(g * 100) / 100 : null,
      gBreak: g !== null && inBreak ? Math.round(g * 100) / 100 : null,
      // A Roll of 13000 deg (seen on the ground in real recordings) is
      // corrupt, not a bank; keep the axis sane.
      bank: roll !== null && Math.abs(roll) <= 180 ? Math.round(Math.abs(roll)) : null,
    };
  });
}

/** Whether there is a load-factor series worth a chart at all. */
export function hasLoadFactor(rows: Row[]): boolean {
  return rows.some((r) => r.g !== null);
}

const AXIS_STYLE = { fill: "var(--text-dim)", fontSize: 11 };
const TOOLTIP_STYLE = {
  backgroundColor: "var(--bg-panel)",
  border: "1px solid var(--border)",
  color: "var(--text)",
};

export function TimeSeriesChart({ track, metrics }: TimeSeriesChartProps) {
  const rows = useMemo(() => buildRows(track, metrics), [track, metrics]);

  if (rows.length === 0) {
    return <p className="empty-message">時系列データがありません。</p>;
  }
  const showLoad = hasLoadFactor(rows);
  const showBank = rows.some((r) => r.bank !== null);
  const showBreak = rows.some((r) => r.gBreak !== null);

  return (
    <div className="timeseries">
      <section>
        <h3>偏差（グライドスロープ / センターライン）</h3>
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke="var(--border-soft)" strokeDasharray="3 3" />
            <XAxis
              dataKey="t"
              tick={AXIS_STYLE}
              label={{ value: "接地前の時間 (秒)", position: "insideBottom", offset: -2, fill: "var(--text-dim)", fontSize: 11 }}
            />
            <YAxis tick={AXIS_STYLE} unit="ft" width={56} />
            <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v) => `${v} ft`} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line name="GS 偏差" dataKey="gs" stroke="var(--series-gs)" dot={false} connectNulls />
            <Line name="CL 偏差" dataKey="cl" stroke="var(--series-cl)" dot={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </section>

      <section>
        <h3>速度</h3>
        <ResponsiveContainer width="100%" height={180}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke="var(--border-soft)" strokeDasharray="3 3" />
            <XAxis dataKey="t" tick={AXIS_STYLE} />
            <YAxis tick={AXIS_STYLE} unit="kt" width={56} />
            <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v) => `${v} kt`} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line name="対気速度" dataKey="kt" stroke="var(--series-speed)" dot={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </section>

      <section>
        <h3>降下率（AGL 差分から算出）</h3>
        <ResponsiveContainer width="100%" height={180}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke="var(--border-soft)" strokeDasharray="3 3" />
            <XAxis dataKey="t" tick={AXIS_STYLE} />
            <YAxis tick={AXIS_STYLE} unit="fpm" width={64} />
            <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v) => `${v} fpm`} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line name="降下率" dataKey="fpm" stroke="var(--series-descent)" dot={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </section>

      {showLoad && (
        <section>
          {/* 軌跡の 2 階微分から導いた法線荷重倍数 (backend:
              app.grading.kinematics)。ブレイク区間は別系列で上に重ねる。
              バンク角は記録に Roll があるときだけ右軸に出す。 */}
          <h3>荷重倍数（G）{showBank ? " とバンク角" : ""}</h3>
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--border-soft)" strokeDasharray="3 3" />
              <XAxis dataKey="t" tick={AXIS_STYLE} />
              <YAxis yAxisId="g" tick={AXIS_STYLE} unit="G" width={56} domain={[0, "auto"]} />
              {showBank && (
                <YAxis
                  yAxisId="bank"
                  orientation="right"
                  tick={AXIS_STYLE}
                  unit="°"
                  width={48}
                  domain={[0, 90]}
                />
              )}
              <Tooltip contentStyle={TOOLTIP_STYLE} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Line
                yAxisId="g"
                name="荷重倍数"
                dataKey="g"
                stroke="var(--series-load)"
                dot={false}
                connectNulls
                unit=" G"
              />
              {showBreak && (
                <Line
                  yAxisId="g"
                  name="ブレイク中"
                  dataKey="gBreak"
                  stroke="var(--leg-break)"
                  strokeWidth={2.5}
                  dot={false}
                  unit=" G"
                />
              )}
              {showBank && (
                <Line
                  yAxisId="bank"
                  name="バンク角"
                  dataKey="bank"
                  stroke="var(--series-bank)"
                  strokeDasharray="4 3"
                  dot={false}
                  connectNulls
                  unit="°"
                />
              )}
            </LineChart>
          </ResponsiveContainer>
        </section>
      )}
    </div>
  );
}
