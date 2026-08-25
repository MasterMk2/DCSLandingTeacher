/** Display formatting helpers (Japanese UI). */

import type { LandingSummary } from "../types/api";

const MS_TO_KNOTS = 3600 / 1852;
const MS_TO_FPM = 60 / 0.3048;
const M_TO_FT = 1 / 0.3048;
const M_TO_NM = 1 / 1852;

export function msToKnots(ms: number): number {
  return ms * MS_TO_KNOTS;
}

export function msToFpm(ms: number): number {
  return ms * MS_TO_FPM;
}

/** Convert meters to feet (Issue D-4). */
export function mToFt(meters: number): number {
  return meters * M_TO_FT;
}

/** Convert meters to nautical miles (Issue D-4). */
export function mToNm(meters: number): number {
  return meters * M_TO_NM;
}

/**
 * Unit-aware rendering of one grading evidence / metric entry (Issue D-4).
 *
 * The backend keeps every value SI and encodes the unit in the key suffix
 * (`_m`, `_ms`, `_fpm`, `_deg`, `_s`). Display converts to the aviation units
 * used elsewhere in the UI and drops the now-stale suffix, so a label can
 * never contradict the value it labels.
 *
 * `_ms` is ambiguous in the payload: descent rates and horizontal speeds are
 * both metres per second, but read as fpm and knots respectively.
 */
export function formatMetric(key: string, value: unknown): { label: string; text: string } {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return {
      label: key,
      text: value === null || value === undefined ? "-" : String(value),
    };
  }
  if (key.endsWith("_ms")) {
    const label = key.slice(0, -"_ms".length);
    return /descent|sink/.test(label)
      ? { label, text: `${Math.round(msToFpm(value))} fpm` }
      : { label, text: `${Math.round(msToKnots(value))} kt` };
  }
  if (key.endsWith("_fpm")) {
    return { label: key.slice(0, -"_fpm".length), text: `${Math.round(value)} fpm` };
  }
  if (key.endsWith("_m")) {
    return { label: key.slice(0, -"_m".length), text: `${Math.round(mToFt(value))} ft` };
  }
  if (key.endsWith("_deg")) {
    return { label: key.slice(0, -"_deg".length), text: `${value.toFixed(1)}°` };
  }
  if (key.endsWith("_s")) {
    return { label: key.slice(0, -"_s".length), text: `${value.toFixed(1)} s` };
  }
  return {
    label: key,
    text: Number.isInteger(value) ? String(value) : value.toFixed(2),
  };
}

/** Format epoch seconds as a localized Japanese datetime string. */
export function formatEpoch(epochSeconds: number | null | undefined): string {
  if (epochSeconds === null || epochSeconds === undefined || !Number.isFinite(epochSeconds)) {
    return "-";
  }
  const d = new Date(epochSeconds * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  );
}

/** Format an ISO-8601 datetime string for display. */
export function formatIso(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return formatEpoch(d.getTime() / 1000);
}

export function kindLabel(kind: string | null | undefined): string {
  switch (kind) {
    case "carrier":
      return "空母着艦";
    case "land":
      return "陸上着陸";
    default:
      return kind ?? "-";
  }
}

export function outcomeLabel(outcome: string | null | undefined): string {
  switch (outcome) {
    case "full_stop":
      return "フルストップ";
    case "touch_and_go":
      return "タッチアンドゴー";
    case "bolter":
      return "ボルター";
    default:
      return outcome ?? "-";
  }
}

/** CSS class for grade coloring. */
export function gradeClass(grade: string | null | undefined): string {
  if (!grade) return "grade-none";
  const g = grade.trim();
  if (/^OK/i.test(g)) return g.includes("-") ? "grade-ok-minus" : "grade-ok";
  if (g.startsWith("(")) return "grade-paren-ok";
  if (/_?NO_GRADE_?/i.test(g)) return "grade-no-grade";
  if (/^CUT/i.test(g)) return "grade-cut";
  return "grade-other";
}

/** Japanese descriptions for LSO / landing factors. */
const FACTOR_DESCRIPTIONS: Record<string, string> = {
  ARCON: "進入高度過高（アプローチ・コントロール）",
  AOC: "センターライン外（アプローチ・オン・センター不足）",
  AOS: "グライドスロープ外（アプローチ・オン・スロープ不足）",
  FAST: "進入速度過大",
  SLOW: "進入速度不足",
  HIGH: "グライドスロープ上方",
  LOW: "グライドスロープ下方",
  OFFLINE: "センターラインから大きく外れる",
  BOLTER: "ボルター（着艦失敗・再進入）",
  WOW: "甲板接地（Weight on Wheels）",
  INTAKE: "インテーク（着艦区域手前接地）",
  IMMAT: "不適切な着艦姿勢",
  "T&R": "タッチ・アンド・ラン（T&R）",
  NWS: "ノーズホイール・ステアリング使用",
  OPEN: "スロットル操作不良（オープン）",
  POWER: "パワー不足",
  BURBLE: "甲板風乱流（バーブル）の影響",
};

export function factorDescription(name: string): string {
  return FACTOR_DESCRIPTIONS[name] ?? "";
}

/** Short label shown in the summary header. */
export function pilotLabel(item: Pick<LandingSummary, "pilot">): string {
  return item.pilot ?? "不明なプレイヤー";
}
