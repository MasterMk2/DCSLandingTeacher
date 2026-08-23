/**
 * GCA (PAR) radar-scope style views (FR-5).
 * Two SVG scopes: azimuth (centerline deviation vs range) and elevation
 * (glideslope deviation vs range), with distance rings / markers and the
 * ideal-course line. The actual approach track is drawn as a dotted trail.
 */

import { useMemo } from "react";
import {
  azimuthTrackPoints,
  computeMaxDeviation,
  computeMaxRange,
  distanceRings,
  elevationTrackPoints,
  scopeScaleX,
  type ScopePoint,
} from "../lib/gcaGeometry";
import type { DeviationSample } from "../types/api";

const WIDTH = 340;
const HEIGHT = 460;
const PAD_X = 44;
const PAD_TOP = 30;
const PAD_BOTTOM = 34;

function toPixel(p: ScopePoint): { px: number; py: number } {
  return {
    px: PAD_X + p.x * (WIDTH - PAD_X * 2),
    py: PAD_TOP + p.y * (HEIGHT - PAD_TOP - PAD_BOTTOM),
  };
}

interface ScopeSvgProps {
  title: string;
  xLabel: string;
  maxDev: number;
  maxRange: number;
  points: ScopePoint[];
}

function ScopeSvg({ title, xLabel, maxDev, maxRange, points }: ScopeSvgProps) {
  const rings = useMemo(() => distanceRings(maxRange, niceRingStep(maxRange)), [maxRange]);
  const track = points.map(toPixel);

  // Ideal course: vertical center line.
  const centerX = toPixel({ x: 0.5, y: 0 }).px;
  const topY = toPixel({ x: 0.5, y: 0 }).py;
  const bottomY = toPixel({ x: 0.5, y: 1 }).py;

  const ringLabels = [...rings, maxRange];

  return (
    <figure className="gca-scope" aria-label={title}>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        className="scope-svg"
        data-testid="gca-scope-svg"
      >
        {/* Scope background */}
        <rect
          x={PAD_X}
          y={PAD_TOP}
          width={WIDTH - PAD_X * 2}
          height={HEIGHT - PAD_TOP - PAD_BOTTOM}
          className="scope-bg"
          rx={6}
        />

        {/* Distance rings (horizontal lines across the scope) */}
        {rings.map((d) => {
          const y = toPixel({ x: 0.5, y: 1 - d / maxRange }).py;
          return (
            <line
              key={`ring-${d}`}
              x1={PAD_X}
              y1={y}
              x2={WIDTH - PAD_X}
              y2={y}
              className="scope-ring"
            />
          );
        })}

        {/* Distance labels */}
        {ringLabels.map((d) => {
          const y = toPixel({ x: 0.5, y: 1 - d / maxRange }).py;
          return (
            <text key={`label-${d}`} x={PAD_X - 6} y={y + 3} textAnchor="end" className="scope-label">
              {(d / 1000).toFixed(1)}km
            </text>
          );
        })}

        {/* Ideal course line */}
        <line
          x1={centerX}
          y1={topY}
          x2={centerX}
          y2={bottomY}
          className="scope-ideal"
          strokeDasharray="8 5"
        />

        {/* Deviation scale ticks */}
        {[-1, -0.5, 0, 0.5, 1].map((t) => {
          const x = toPixel({ x: scopeScaleX(t * maxDev, maxDev), y: 0 }).px;
          return (
            <text key={`tick-${t}`} x={x} y={HEIGHT - PAD_BOTTOM + 14} textAnchor="middle" className="scope-label">
              {t === 0 ? "0" : `${t > 0 ? "+" : ""}${Math.round(t * maxDev)}m`}
            </text>
          );
        })}

        {/* Approach trail: dotted points + connecting polyline */}
        {track.length > 1 && (
          <polyline
            points={track.map((p) => `${p.px},${p.py}`).join(" ")}
            className="scope-trail-line"
          />
        )}
        {track.map((p, i) => (
          <circle key={i} cx={p.px} cy={p.py} r={2.2} className="scope-trail-dot" />
        ))}

        {/* Touchdown marker */}
        <circle cx={centerX} cy={bottomY} r={4} className="scope-touchdown" />

        <text x={WIDTH / 2} y={16} textAnchor="middle" className="scope-title">
          {title}
        </text>
        <text x={WIDTH / 2} y={HEIGHT - 4} textAnchor="middle" className="scope-label">
          {xLabel}
        </text>
      </svg>
    </figure>
  );
}

function niceRingStep(maxRange: number): number {
  if (maxRange <= 1000) return 250;
  if (maxRange <= 2000) return 500;
  return 1000;
}

export interface GcaScopeProps {
  samples: DeviationSample[];
}

export function GcaScope({ samples }: GcaScopeProps) {
  const azDev = useMemo(
    () => computeMaxDeviation(samples, (s) => s.centerline_deviation),
    [samples],
  );
  const elDev = useMemo(
    () => computeMaxDeviation(samples, (s) => s.glideslope_deviation),
    [samples],
  );
  const maxRange = useMemo(() => computeMaxRange(samples), [samples]);

  const azPoints = useMemo(() => azimuthTrackPoints(samples, azDev, maxRange), [samples, azDev, maxRange]);
  const elPoints = useMemo(() => elevationTrackPoints(samples, elDev, maxRange), [samples, elDev, maxRange]);

  return (
    <div className="gca-scopes">
      <ScopeSvg
        title="方位角スコープ"
        xLabel="センターライン偏差（右 +）"
        maxDev={azDev}
        maxRange={maxRange}
        points={azPoints}
      />
      <ScopeSvg
        title="仰角スコープ"
        xLabel="グライドスロープ偏差（上 +）"
        maxDev={elDev}
        maxRange={maxRange}
        points={elPoints}
      />
    </div>
  );
}
