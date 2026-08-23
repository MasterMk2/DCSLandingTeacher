/** Top-down plan-view of the approach track (course frame, no map). */

import { useMemo } from "react";
import {
  computeMaxDeviation,
  computeMaxRange,
  topDownPoints,
} from "../lib/gcaGeometry";
import type { DeviationSample } from "../types/api";

const WIDTH = 480;
const HEIGHT = 480;
const PAD = 46;

export interface TopDownTrackProps {
  samples: DeviationSample[];
}

export function TopDownTrack({ samples }: TopDownTrackProps) {
  const maxLateral = useMemo(
    () => computeMaxDeviation(samples, (s) => s.centerline_deviation, 50),
    [samples],
  );
  const maxRange = useMemo(() => computeMaxRange(samples), [samples]);

  const points = useMemo(
    () => topDownPoints(samples, maxLateral, maxRange),
    [samples, maxLateral, maxRange],
  );

  const toPx = (x: number, y: number) => ({
    px: PAD + x * (WIDTH - PAD * 2),
    py: PAD + y * (HEIGHT - PAD * 2),
  });

  const track = points.map((p) => toPx(p.x, p.y));
  const centerX = toPx(0.5, 0).px;
  const topY = toPx(0.5, 0).py;
  const bottomY = toPx(0.5, 1).py;

  return (
    <figure className="topdown-view" aria-label="トップダウン軌跡">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" className="scope-svg">
        <rect
          x={PAD}
          y={PAD}
          width={WIDTH - PAD * 2}
          height={HEIGHT - PAD * 2}
          className="scope-bg"
          rx={6}
        />

        {/* Runway / centerline */}
        <line
          x1={centerX}
          y1={topY}
          x2={centerX}
          y2={bottomY}
          className="scope-ideal"
          strokeDasharray="10 6"
        />
        {/* Threshold bar at touchdown end */}
        <line
          x1={PAD + 8}
          y1={bottomY}
          x2={WIDTH - PAD - 8}
          y2={bottomY}
          className="scope-threshold"
        />

        {/* Lateral scale */}
        {[-1, -0.5, 0, 0.5, 1].map((t) => {
          const x = toPx(0.5 + t / 2, 0).px;
          return (
            <text key={`t-${t}`} x={x} y={HEIGHT - PAD + 16} textAnchor="middle" className="scope-label">
              {t === 0 ? "0" : `${t > 0 ? "+" : ""}${Math.round(t * maxLateral)}m`}
            </text>
          );
        })}
        <text x={centerX} y={PAD - 8} textAnchor="middle" className="scope-label">
          最終進入（{maxRange >= 1000 ? `${(maxRange / 1000).toFixed(1)}km` : `${Math.round(maxRange)}m`}）
        </text>

        {track.length > 1 && (
          <polyline
            points={track.map((p) => `${p.px},${p.py}`).join(" ")}
            className="scope-trail-line"
          />
        )}
        {track.map((p, i) => (
          <circle key={i} cx={p.px} cy={p.py} r={2.4} className="scope-trail-dot" />
        ))}
        <circle cx={centerX} cy={bottomY} r={4} className="scope-touchdown" />

        <text x={WIDTH / 2} y={18} textAnchor="middle" className="scope-title">
          トップダウン軌跡（進入コース平面図）
        </text>
      </svg>
    </figure>
  );
}
