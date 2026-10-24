/** Time-series charts: deviations, speed, AOA, descent rate and load factor (Recharts). */

import { useMemo } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { mToFt, msToFpm, msToKnots } from "../lib/format";
import { descentRateSmoothed, robustSmooth } from "../lib/smoothing";
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
  /** Angle of attack, degrees: the recorded one when the recording has it
   *  (client-side recordings), otherwise the one estimated from the track
   *  and the attitude -- the same choice the grader makes. */
  aoa: number | null;
}

/** The on-speed AOA band the grade was judged against, when there is one. */
export function aoaBand(
  metrics?: Record<string, unknown> | null,
): { min: number; max: number } | null {
  const min = num(metrics?.["aoa_on_speed_min_deg"]);
  const max = num(metrics?.["aoa_on_speed_max_deg"]);
  return min !== null && max !== null ? { min, max } : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Half-width (s) of the smoothing window for speed and descent rate
 *  (lib/smoothing.ts explains why they need one). */
export const SMOOTHING_HALF_WINDOW_S = 1.0;

/**
 * Run `smooth` separately on the samples before and from `splitTime`.
 *
 * The windows must not reach across the touchdown: a trap stops the jet
 * from 65 m/s in about two seconds, and the sink stops at the wheels, so a
 * window straddling that instant would draw the deceleration (or the end of
 * the descent) seconds before it happened -- right where the LSO's FAST /
 * SLOW and the touchdown sink are judged.
 */
function smoothEachSide(
  times: number[],
  values: (number | null)[],
  splitTime: number,
  smooth: (t: number[], v: (number | null)[]) => (number | null)[],
): (number | null)[] {
  const split = times.findIndex((t) => t >= splitTime);
  if (split <= 0) return smooth(times, values);
  return [
    ...smooth(times.slice(0, split), values.slice(0, split)),
    ...smooth(times.slice(split), values.slice(split)),
  ];
}

export function buildRows(
  track: ApproachTrack,
  metrics?: Record<string, unknown> | null,
): Row[] {
  const tdTime = track.touchdown_time ?? track.samples.at(-1)?.time ?? 0;
  const times = track.samples.map((s) => s.time);
  const speeds = smoothEachSide(times, track.samples.map((s) => num(s.speed)), tdTime, (t, v) =>
    robustSmooth(t, v, SMOOTHING_HALF_WINDOW_S),
  );
  const rates = smoothEachSide(times, track.samples.map((s) => num(s.agl)), tdTime, (t, v) =>
    descentRateSmoothed(t, v, SMOOTHING_HALF_WINDOW_S),
  );
  const breakStart = num(metrics?.["pattern_break_start_time"]);
  const breakEnd = num(metrics?.["pattern_break_end_time"]);
  // Recorded or estimated for the whole series, never a mix (backend
  // app.grading.aoa.aoa_values makes the same choice).
  const recorded = track.samples.some((s) => num(s.aoa) !== null);
  return track.samples.map((s, i) => {
    const g = num(s.load_factor);
    const aoa = recorded ? num(s.aoa) : num(s.aoa_est);
    const speed = speeds[i];
    const rate = rates[i];
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
      kt: speed !== null ? Math.round(msToKnots(speed)) : null,
      fpm: rate !== null ? Math.round(msToFpm(rate)) : null,
      g: g !== null ? Math.round(g * 100) / 100 : null,
      gBreak: g !== null && inBreak ? Math.round(g * 100) / 100 : null,
      // A Roll of 13000 deg (seen on the ground in real recordings) is
      // corrupt, not a bank; keep the axis sane.
      bank: roll !== null && Math.abs(roll) <= 180 ? Math.round(Math.abs(roll)) : null,
      aoa: aoa !== null ? Math.round(aoa * 10) / 10 : null,
    };
  });
}

/** Whether there is a load-factor series worth a chart at all. */
export function hasLoadFactor(rows: Row[]): boolean {
  return rows.some((r) => r.g !== null);
}

const AXIS_STYLE = { fill: "var(--text-dim)", fontSize: 11 };

/** Time on a numeric axis, not a category axis. A category axis spaces the
 *  samples evenly whatever their timestamps, so a recording whose interval
 *  varies (0.13 s here, 1 s there) was drawn with its time axis stretched
 *  and squeezed -- kinks that were not in the flight -- and repeated tick
 *  labels ("-4.9, -4.9"). */
const TIME_AXIS = {
  dataKey: "t",
  type: "number" as const,
  domain: ["dataMin", "dataMax"] as [string, string],
  tick: AXIS_STYLE,
  tickFormatter: (v: number) => String(Math.round(v)),
};

/** Round-second ticks across the record (a numeric axis left to itself
 *  labels its ends, e.g. -109 and 5, and little between). */
export function timeTicks(rows: Row[], maxTicks = 12): number[] {
  if (rows.length === 0) return [];
  const first = rows[0].t;
  const last = rows[rows.length - 1].t;
  const step =
    [1, 2, 5, 10, 15, 20, 30, 60, 120, 300].find((s) => (last - first) / s <= maxTicks) ?? 600;
  const ticks: number[] = [];
  for (let t = Math.ceil(first / step) * step; t <= last; t += step) ticks.push(t);
  return ticks;
}

function timeLabel(value: unknown): string {
  return typeof value === "number" ? `接地 ${value > 0 ? "+" : ""}${value.toFixed(1)} 秒` : "";
}

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
  const showAoa = rows.some((r) => r.aoa !== null);
  const band = aoaBand(metrics);
  const ticks = timeTicks(rows);

  return (
    <div className="timeseries">
      <section>
        <h3>偏差（グライドスロープ / センターライン）</h3>
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke="var(--border-soft)" strokeDasharray="3 3" />
            <XAxis
              {...TIME_AXIS} ticks={ticks}
              label={{ value: "接地前の時間 (秒)", position: "insideBottom", offset: -2, fill: "var(--text-dim)", fontSize: 11 }}
            />
            <YAxis tick={AXIS_STYLE} unit="ft" width={56} />
            <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={timeLabel} formatter={(v) => `${v} ft`} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line name="GS 偏差" dataKey="gs" stroke="var(--series-gs)" dot={false} connectNulls />
            <Line name="CL 偏差" dataKey="cl" stroke="var(--series-cl)" dot={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </section>

      <section>
        <h3>速度（2 秒平滑。サーバーの記録では対地速度）</h3>
        <ResponsiveContainer width="100%" height={180}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke="var(--border-soft)" strokeDasharray="3 3" />
            <XAxis {...TIME_AXIS} ticks={ticks} />
            <YAxis tick={AXIS_STYLE} unit="kt" width={56} />
            <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={timeLabel} formatter={(v) => `${v} kt`} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line name="速度" dataKey="kt" stroke="var(--series-speed)" dot={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </section>

      {showAoa && (
        <section>
          {/* 記録に AOA が無い (DCS サーバーの記録はすべてそう) ときは、
              軌跡と姿勢から推定した値 (backend: app.grading.aoa)。帯は
              採点に使った機体別のオンスピード範囲。 */}
          <h3>
            迎角
            {band ? `（網掛け = オンスピード ${band.min.toFixed(1)}–${band.max.toFixed(1)}°）` : ""}
          </h3>
          <ResponsiveContainer width="100%" height={180}>
            <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--border-soft)" strokeDasharray="3 3" />
              <XAxis {...TIME_AXIS} ticks={ticks} />
              <YAxis tick={AXIS_STYLE} unit="°" width={56} domain={["auto", "auto"]} />
              {band && (
                <ReferenceArea
                  y1={band.min}
                  y2={band.max}
                  fill="var(--series-aoa)"
                  fillOpacity={0.15}
                  ifOverflow="extendDomain"
                />
              )}
              <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={timeLabel} formatter={(v) => `${v}°`} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Line name="迎角" dataKey="aoa" stroke="var(--series-aoa)" dot={false} connectNulls />
            </LineChart>
          </ResponsiveContainer>
        </section>
      )}

      <section>
        <h3>降下率（高度の 2 秒窓の傾きから算出）</h3>
        <ResponsiveContainer width="100%" height={180}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke="var(--border-soft)" strokeDasharray="3 3" />
            <XAxis {...TIME_AXIS} ticks={ticks} />
            <YAxis tick={AXIS_STYLE} unit="fpm" width={64} />
            <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={timeLabel} formatter={(v) => `${v} fpm`} />
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
              <XAxis {...TIME_AXIS} ticks={ticks} />
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
              <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={timeLabel} />
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
