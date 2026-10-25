/**
 * Glideslope Profile Chart - vertical cross-section of the approach: height
 * against distance, the reference glidepath and, with a resolved runway, the
 * straight line that was actually flown.
 *
 * Three frames, because the backend measures three kinds of track:
 *
 * - carrier (moving deck): distance to the target wire, height above the deck;
 * - runway: distance to the THRESHOLD, with the runway itself to the right of
 *   zero. The reference glidepath meets the runway at the reference point
 *   (DCS's ILS glidepath intercept where the seed knows it, else the fixed
 *   aiming point) and the flown straight line meets it wherever the pilot
 *   aimed; drawing both to the ground is what makes "3.0 deg, aimed 170 m
 *   short" visible as two parallel lines instead of a mysterious offset;
 * - touchdown (no runway resolved, older rows): distance to the touchdown
 *   point, as before.
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
import type { ApproachTrack, DeviationSample } from "../types/api";

/** Drawn at a fixed size and scaled by CSS: see the note at the chart. */
const CHART_WIDTH = 760;
const CHART_HEIGHT = 320;
const M_PER_NM = 1852;
const DEG = Math.PI / 180;

export interface GlideslopeProfileChartProps {
  track: ApproachTrack;
  /** Grading metrics; carries the roll-out time used for the base->final mark
   *  and the fitted glidepath (angle, aim point) drawn as the flown line. */
  metrics?: Record<string, unknown> | null;
}

const num = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

/** Which distance the x axis measures. */
export type ProfileFrame = "carrier" | "runway" | "touchdown";

/** Is this a carrier track measured against the moving deck?
 *
 * Only those are referenced to the target wire and to the deck. Carrier
 * rows stored before that change carry no `frame`: they were measured from
 * the ramp (or the touchdown point) with a sea-referenced height and a deck
 * frozen at the touchdown instant, so the carrier wording would misdescribe
 * them. */
export function isMovingDeck(track: ApproachTrack): boolean {
  return track.geometry?.["frame"] === "moving_deck";
}

export function profileFrame(track: ApproachTrack): ProfileFrame {
  if (isMovingDeck(track)) return "carrier";
  // A runway frame needs the per-sample threshold distance; rows stored
  // before it existed fall back to the touchdown frame they were built in.
  if (
    track.kind === "land" &&
    track.geometry?.["kind"] === "runway" &&
    track.samples.some((s) => num(s.distance_to_threshold) !== null)
  ) {
    return "runway";
  }
  return "touchdown";
}

/** Metres along the course the x axis uses for a sample. */
export function profileDistanceM(sample: DeviationSample, frame: ProfileFrame): number | null {
  return frame === "runway" ? num(sample.distance_to_threshold) : num(sample.distance_to_go);
}

/** Where the reference glidepath meets the surface, metres past the threshold
 *  (runway frame). The backend stores it as `aiming_point_m` whatever decided
 *  it; `aiming_point_source` says whether that was DCS's ILS or the default. */
export function referenceOriginM(track: ApproachTrack): number {
  return num(track.geometry?.["aiming_point_m"]) ?? 0;
}

/** The straight line fitted to the graded final, from the metrics: its angle
 *  and where it meets the runway (metres past the threshold). Null when the
 *  grader did not produce one (no runway, too short a final, older rows). */
export function flownLine(
  metrics?: Record<string, unknown> | null,
): { angleDeg: number; aimFromThresholdM: number } | null {
  const angle = num(metrics?.["mean_path_angle_deg"]);
  const aim = num(metrics?.["glidepath_aim_point_from_threshold_m"]);
  if (angle === null || aim === null || angle <= 0) return null;
  return { angleDeg: angle, aimFromThresholdM: aim };
}

/** Distance (nm, in the chart's frame) at the roll-out onto final, or null.
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
  const touchdown = num(track.touchdown_time);
  let time = num(metrics?.["pattern_rollout_time"]);
  if (time === null && touchdown !== null) {
    // Straight-in: no turn to roll out of, so fall back to where the graded
    // final began (the stabilization gate).
    const window = num(metrics?.["final_window_s"]);
    if (window !== null) time = touchdown - window;
  }
  if (time === null) return null;

  const frame = profileFrame(track);
  let best: number | null = null;
  let bestGap = Infinity;
  for (const sample of track.samples) {
    const gap = Math.abs(sample.time - time);
    const distance = profileDistanceM(sample, frame);
    if (gap < bestGap && distance !== null) {
      bestGap = gap;
      best = distance;
    }
  }
  // A match many seconds away means the sample simply is not in the record.
  if (best === null || bestGap > 3.0) return null;
  return mToNm(best);
}

export interface ProfilePoint {
  distance_nm: number;       // Distance along the course in the chart's frame (nm)
  agl_ft: number | null;     // Actual height above the landing surface (ft)
  ideal_ft: number | null;   // Reference glidepath height (ft)
  flown_ft: number | null;   // Straight line fitted to the final (ft), runway frame only
  glideslope_dev_ft: number | null;  // Glideslope deviation (ft)
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

/** Height (ft) the glideslope ends at above the landing surface.
 *
 * Zero on land. On a carrier the FLOLS glidepath ends at the target wire a
 * couple of metres up -- the aircraft's reference point sits that high when
 * its wheels are on the deck (`reference_height_m`, AIRBOSS: 2 m) -- and
 * the backend's deviations are measured from that line, so the drawn ideal
 * has to be too. */
function glideslopeEndFt(track: ApproachTrack): number {
  const value = track.geometry?.["reference_height_m"];
  return typeof value === "number" && Number.isFinite(value) ? mToFt(value) : 0;
}

/** Reference glidepath height (ft) at a distance (m) in the chart's frame. */
function idealFt(distanceM: number, track: ApproachTrack, frame: ProfileFrame): number {
  const tanSlope = Math.tan((track.glideslope_deg ?? 3.5) * DEG);
  // In the runway frame the origin sits `referenceOriginM` past the threshold,
  // i.e. at x = -origin; the path is clamped to the ground beyond it.
  const toOrigin = frame === "runway" ? distanceM + referenceOriginM(track) : distanceM;
  return mToFt(Math.max(0, toOrigin) * tanSlope) + glideslopeEndFt(track);
}

export function buildProfilePoints(
  track: ApproachTrack,
  metrics?: Record<string, unknown> | null,
): ProfilePoint[] {
  const frame = profileFrame(track);
  const fitted = frame === "runway" ? flownLine(metrics) : null;
  const touchdown = num(track.touchdown_time);

  const points: ProfilePoint[] = [];
  for (const s of track.samples) {
    const distanceM = profileDistanceM(s, frame);
    if (distanceM === null) continue;
    if (frame === "runway") {
      // Over the runway up to the touchdown; the ground roll is not an approach.
      if (touchdown !== null && s.time > touchdown) continue;
    } else if (distanceM < 0) {
      continue;
    }
    const agl = num(s.agl);
    const dev = num(s.glideslope_deviation);
    points.push({
      distance_nm: mToNm(distanceM),
      agl_ft: agl !== null ? mToFt(agl) : null,
      ideal_ft: idealFt(distanceM, track, frame),
      flown_ft:
        fitted !== null
          ? mToFt(Math.max(0, distanceM - fitted.aimFromThresholdM) * Math.tan(fitted.angleDeg * DEG))
          : null,
      glideslope_dev_ft: dev !== null ? mToFt(dev) : null,
    });
  }
  return points;
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

/** The reference and flown lines carried down to where they meet the surface.
 *
 *  The sample points only reach as far as the aircraft flew; the lines'
 *  own end points (the reference origin, the flown line's aim point) are
 *  what the reader compares, so each gets a point of its own with no
 *  aircraft data. Data runs far -> near, so they slot in by distance. */
function withLineEnds(
  points: ProfilePoint[],
  track: ApproachTrack,
  frame: ProfileFrame,
  fitted: { angleDeg: number; aimFromThresholdM: number } | null,
): ProfilePoint[] {
  if (frame !== "runway" || points.length === 0) return points;
  const ends: ProfilePoint[] = [
    {
      distance_nm: mToNm(-referenceOriginM(track)),
      agl_ft: null,
      ideal_ft: glideslopeEndFt(track),
      flown_ft: null,
      glideslope_dev_ft: null,
    },
  ];
  if (fitted !== null) {
    ends.push({
      distance_nm: mToNm(fitted.aimFromThresholdM),
      agl_ft: null,
      ideal_ft: null,
      flown_ft: 0,
      glideslope_dev_ft: null,
    });
  }
  const farthest = points[0].distance_nm;
  const merged = [...points, ...ends.filter((e) => e.distance_nm <= farthest)];
  merged.sort((a, b) => b.distance_nm - a.distance_nm);
  return merged;
}

export function GlideslopeProfileChart({ track, metrics }: GlideslopeProfileChartProps) {
  const frame = profileFrame(track);
  const fitted = frame === "runway" ? flownLine(metrics) : null;
  const points = useMemo(
    () => withLineEnds(inboundLeg(buildProfilePoints(track, metrics)), track, frame, fitted),
    [track, metrics, frame, fitted],
  );
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
  // On the boat the last turn is the 180 and what follows it is the groove;
  // the distances are to the target wire, not to wherever the jet touched.
  const carrier = frame === "carrier";
  const runway = frame === "runway";
  const rolloutLabel = carrier ? "グルーブ開始" : "ベース→ファイナル";
  const distanceLabel = carrier
    ? "目標ワイヤーまでの距離 (nm)"
    : runway
      ? "滑走路末端（閾値）までの距離 (nm)"
      : "接地点までの距離 (nm)";
  const slopeDeg = track.glideslope_deg ?? 3.5;
  const originM = referenceOriginM(track);
  const originSource = track.geometry?.["aiming_point_source"];
  const originLabel = runway
    ? `基準点 閾値+${Math.round(originM)} m${
        originSource === "ils" ? "（DCS の ILS）" : originSource === "default" ? "（既定値）" : ""
      }`
    : "";

  if (points.length === 0) {
    return <p className="empty-message">グライドスローププロファイルデータがありません。</p>;
  }

  // Fit the axes to the approach that was actually flown. Fixed floors (the
  // 2.5 nm / 2000 ft this used to force) squeeze a short approach -- a
  // helicopter, or a recording that only caught short final -- into a sliver
  // at the edge of an otherwise empty plot.
  const maxDist = Math.max(...points.map(p => p.distance_nm), 0.2);
  const minDist = Math.min(...points.map(p => p.distance_nm), 0);
  const idealAtStart = idealFt(maxDist * M_PER_NM, track, frame);
  const maxAgl =
    Math.max(
      ...points.filter(p => p.agl_ft !== null).map(p => p.agl_ft as number),
      idealAtStart,
      50
    ) * 1.15;
  const aglAxis = niceAxis(maxAgl);
  const distAxis = niceAxis(maxDist);
  // The runway frame runs past zero onto the runway (negative x). Give that
  // side one round tick so the touchdown and the reference point are readable.
  const nearAxis = minDist < 0 ? niceAxis(-minDist, 1) : null;
  const distTicks = nearAxis ? [-nearAxis.max, ...distAxis.ticks] : distAxis.ticks;
  const distMin = nearAxis ? -nearAxis.max : 0;
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
  // Where the aircraft touched down, in the chart's frame (runway: the last
  // sample before the touchdown time; otherwise the origin itself).
  const touchdownNm = runway
    ? points.filter((p) => p.agl_ft !== null).at(-1)?.distance_nm ?? null
    : 0;
  const tickDecimals = distAxis.max < 0.5 ? 2 : 1;

  return (
    <div className="glideslope-profile-chart">
      <h3>グライドスロープ プロファイル（横断面図）</h3>
      <div className="chart-legend">
        <span className="legend-item">
          <span className="legend-color ideal"></span>
          理想グライドパス ({slopeDeg.toFixed(1)}°){runway && `、${originLabel}`}
        </span>
        <span className="legend-item"><span className="legend-color actual"></span>実飛行AGL (ft)</span>
        {fitted !== null && (
          <span className="legend-item">
            <span className="legend-color flown"></span>
            飛んだ直線（{fitted.angleDeg.toFixed(1)}°、閾値{fitted.aimFromThresholdM >= 0 ? "+" : "-"}
            {Math.abs(Math.round(fitted.aimFromThresholdM))} m）
          </span>
        )}
        <span className="legend-item"><span className="legend-color deviation"></span>グライドスロープ偏差 (ft, 右軸)</span>
        {rolloutNm !== null && (
          <span className="legend-item"><span className="legend-color rollout"></span>{rolloutLabel}（ここでグライドスロープに乗る）</span>
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
          <CartesianGrid stroke="var(--border-soft)" strokeDasharray="3 3" />
          <XAxis
            dataKey="distance_nm"
            type="number"
            domain={[distMin, distAxis.max]}
            ticks={distTicks}
            // `reversed`, not a descending domain: Recharts ignores the
            // latter, which had the approach running touchdown-first and
            // contradicting this chart's own caption.
            reversed
            tick={{ fill: "var(--text-dim)", fontSize: 11 }}
            label={{
              value: distanceLabel,
              position: "insideBottom",
              offset: -10,
              fill: "var(--text-dim)",
              fontSize: 11,
            }}
            tickFormatter={(v) => v.toFixed(tickDecimals)}
          />
          <YAxis
            yAxisId="left"
            tick={{ fill: "var(--text-dim)", fontSize: 11 }}
            label={{
              value: "高度 / AGL (ft)",
              angle: -90,
              position: "insideLeft",
              offset: 15,
              fill: "var(--text-dim)",
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
            tick={{ fill: "var(--text-dim)", fontSize: 11 }}
            label={{
              value: "GS偏差 (ft)",
              angle: 90,
              position: "insideRight",
              offset: 15,
              fill: "var(--text-dim)",
              fontSize: 11,
            }}
            domain={[-devHalf.max, devHalf.max]}
            ticks={devTicks}
            tickFormatter={(v) => `${Math.round(v)}`}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "var(--bg-panel)",
              border: "1px solid var(--border)",
              color: "var(--text)",
            }}
            formatter={(value: number, name: string) => {
              if (name === "agl_ft" || name === "ideal_ft" || name === "flown_ft") {
                return [`${Math.round(value)} ft`, name];
              }
              if (name === "glideslope_dev_ft") return [`${Math.round(value)} ft`, "GS偏差"];
              return [value, name];
            }}
            labelFormatter={(v) => `${v.toFixed(2)} nm`}
          />
          {/* Reference glidepath */}
          <Line
            name="理想グライドパス"
            dataKey="ideal_ft"
            yAxisId="left"
            stroke="var(--series-cl)"
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
            stroke="var(--series-gs)"
            dot={false}
            connectNulls
            strokeWidth={2}
            isAnimationActive={false}
          />

          {/* The straight line fitted to the graded final: what the glidepath
              score is about (its angle and the scatter around it). Parallel to
              the reference but offset = aimed elsewhere, not flown wrong. */}
          {fitted !== null && (
            <Line
              name="飛んだ直線"
              dataKey="flown_ft"
              yAxisId="left"
              stroke="var(--text)"
              dot={false}
              connectNulls
              strokeWidth={1}
              isAnimationActive={false}
            />
          )}

          {/* Glideslope deviation */}
          <Line
            name="グライドスロープ偏差"
            dataKey="glideslope_dev_ft"
            stroke="var(--series-speed)"
            strokeDasharray="3 3"
            dot={false}
            connectNulls
            strokeWidth={1.5}
            isAnimationActive={false}
            yAxisId="right"
          />

          {/* Threshold (runway frame) and touchdown, in data space (a raw
              <line> would be placed in SVG coordinates and sit at the edge of
              the plot instead). */}
          {runway && (
            <ReferenceLine
              x={0}
              yAxisId="left"
              stroke="var(--text-dim)"
              label={{
                value: "閾値",
                position: "insideTopRight",
                fill: "var(--text-dim)",
                fontSize: 10,
              }}
            />
          )}
          {runway && (
            <ReferenceLine
              x={mToNm(-originM)}
              yAxisId="left"
              stroke="var(--series-cl)"
              strokeDasharray="2 4"
              label={{
                value: "基準点",
                position: "insideBottomRight",
                fill: "var(--series-cl)",
                fontSize: 10,
              }}
            />
          )}
          {touchdownNm !== null && (
            <ReferenceLine
              x={touchdownNm}
              yAxisId="left"
              stroke="var(--danger)"
              strokeDasharray="4 4"
              label={
                runway
                  ? {
                      value: "接地",
                      position: "insideTopLeft",
                      fill: "var(--danger)",
                      fontSize: 10,
                    }
                  : undefined
              }
            />
          )}

          {/* Base -> final. Left of this the aircraft is still turning and
              being off the slope is expected; right of it is what is graded.
              区間の境目は系列ではなく注記なので、系列色ではなく muted で引く。
              系列色を当てると 4 本目のデータ線に見える。 */}
          {rolloutNm !== null && (
            <ReferenceLine
              x={rolloutNm}
              yAxisId="left"
              stroke="var(--text-dim)"
              strokeDasharray="6 4"
              label={{
                value: rolloutLabel,
                position: "insideTopLeft",
                fill: "var(--text-dim)",
                fontSize: 10,
              }}
            />
          )}
        </LineChart>
      </div>
      <p className="chart-note">
        {carrier
          ? "※ 横軸は目標ワイヤー（3 番）までの距離、高さは甲板から。艦と一緒に動く座標で測っている。グライドスロープ偏差は右軸（ft、+ が理想より上）。横ずれはパターン軌跡を参照。"
          : runway
            ? "※ 横軸は滑走路末端（閾値）までの距離で、0 が閾値、右側（負）は滑走路上。理想線は基準点（DCS の ILS グライドパスが滑走路面に達する点。ILS の無い方向は閾値 +300 m の既定値）で地面に達する線。細い実線はファイナルに当てた直線で、採点はこの傾きとばらつき。理想線と平行にずれていれば、角度は合っていて狙点が違う。グライドスロープ偏差は右軸（ft、+ が理想線より上）。"
            : "※ 横軸は接地点までの距離（左：進入開始側 → 右：接地点）。グライドスロープ偏差は右軸（ft、+ が理想より上）。横ずれはパターン軌跡を参照。"}
        {carrier
          ? "紫の破線がグルーブ開始の位置。"
          : "紫の破線がベース→ファイナルの境目で、採点対象はその右側。"}
      </p>
    </div>
  );
}
