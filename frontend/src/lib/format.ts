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
    // 採点項目名の配列 (measured_components など) は JSON のまま出すと
    // 読めないので、日本語名を並べる。
    if (Array.isArray(value) && value.every((v) => typeof v === "string")) {
      const stem = key.replace(/_(ms|fpm|m|deg|s)$/, "");
      const names = (value as string[]).map(factorLabel).join("・");
      return { label: metricLabel(key, stem), text: names || "なし" };
    }
    const text =
      value === null || value === undefined
        ? "-"
        : typeof value === "object"
          // sub_scores / bands_fpm のような入れ子。String() だと
          // "[object Object]" になって根拠が読めなくなる。
          ? JSON.stringify(value)
          : String(value);
    // Strip the unit suffix here too: a metric that came back null still
    // has a Japanese label, and looking up only the full key printed the
    // raw "mean_path_angle_deg" next to a "-".
    const stem = key.replace(/_(ms|fpm|m|deg|s)$/, "");
    return { label: metricLabel(key, stem), text };
  }
  if (key.endsWith("_ms")) {
    const stem = key.slice(0, -"_ms".length);
    const label = metricLabel(key, stem);
    return /descent|sink/.test(stem)
      ? { label, text: `${Math.round(msToFpm(value))} fpm` }
      : { label, text: `${Math.round(msToKnots(value))} kt` };
  }
  if (key.endsWith("_fpm")) {
    const stem = key.slice(0, -"_fpm".length);
    return { label: metricLabel(key, stem), text: `${Math.round(value)} fpm` };
  }
  if (key.endsWith("_m")) {
    const stem = key.slice(0, -"_m".length);
    // パターンの離隔は海里で述べる: 1.5 nm を 9000 ft と言われても
    // 飛んでいる側の感覚と結びつかない。
    const text = /abeam/.test(stem)
      ? `${(value / 1852).toFixed(2)} nm`
      : `${Math.round(mToFt(value))} ft`;
    return { label: metricLabel(key, stem), text };
  }
  if (key.endsWith("_deg")) {
    const stem = key.slice(0, -"_deg".length);
    return { label: metricLabel(key, stem), text: `${value.toFixed(1)}°` };
  }
  if (key.endsWith("_s")) {
    const stem = key.slice(0, -"_s".length);
    return { label: metricLabel(key, stem), text: `${value.toFixed(1)} s` };
  }
  return {
    label: metricLabel(key, key),
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

/** Format an ISO-8601 datetime string for display.
 *
 * A string with no timezone designator is treated as UTC, because that is
 * what the backend sends: it stores naive UTC and FastAPI serialises it
 * without an offset. `new Date()` would read it as local time instead and
 * shift every recorded timestamp by the viewer's offset (9 hours in JST).
 */
export function formatIso(iso: string | null | undefined): string {
  if (!iso) return "-";
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const d = new Date(hasZone ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return iso;
  return formatEpoch(d.getTime() / 1000);
}

/** Mission-clock time of the touchdown, i.e. the date set inside the .miz.
 *
 * Kept separate from the recorded time on purpose: ACMI's ReferenceTime is
 * the simulated world's date (the Caucasus mission here is set in June
 * 2026), so presenting it as "the time of the landing" reads as a bug to
 * anyone who flew it this morning. Imported recordings carry no real-world
 * timestamp at all -- only this one.
 */
export function formatMissionTime(epochSeconds: number | null | undefined): string {
  return formatEpoch(epochSeconds);
}

/** Japanese label for an approach pattern. */
export function patternLabel(pattern: string | null | undefined): string {
  switch (pattern) {
    case "overhead":
      return "オーバーヘッド";
    case "straight_in":
      return "ストレートイン";
    case "unknown":
      return "不明";
    default:
      return "-";
  }
}

/** Badge class for an approach pattern.
 *
 * The stylesheet keys on `pattern-overhead` / `pattern-straight_in` /
 * `pattern-unknown`; interpolating the raw value produces `overhead`, which
 * matches nothing and silently renders an unstyled badge (it did, on the
 * detail page).
 */
export function patternClass(pattern: string | null | undefined): string {
  return pattern ? `pattern-badge pattern-${pattern}` : "pattern-badge pattern-unknown";
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
  // 陸上飛行場の採点コンポーネント (LSO ファクターと違い加点式)。
  descent_rate: "接地降下率（機体クラス別の許容幅で判定）",
  touchdown_speed: "接地速度（ファイナル区間の平均速度に対する比）",
  glideslope: "グライドスロープ追従（ファイナル開始から閾値まで）",
  centerline: "センターライン保持（接地直前）",
  pattern: "オーバーヘッドパターン（旋回明けの軸ずれ / ダウンウィンドの方位・高度）",
};

/** 評価メトリクスの日本語ラベル。無いキーは従来どおりキー名を出す。 */
const METRIC_LABELS: Record<string, string> = {
  touchdown_descent_rate: "接地降下率",
  touchdown_speed: "接地速度",
  mean_approach_speed: "基準速度（ファイナル平均）",
  speed_ratio: "速度比",
  speed_reference: "速度基準区間",
  touchdown_speed_ratio: "速度比",
  verdict: "判定",
  airframe: "機体",
  airframe_class: "機体クラス",
  bands_fpm: "許容幅 (fpm)",
  mean_abs_error: "平均グライドスロープ誤差",
  mean_signed_error: "平均誤差（+ = 高い）",
  mean_abs_deviation: "平均偏差",
  mean_signed_deviation: "平均偏差（+ = 高い）",
  mean_glideslope_error: "平均グライドスロープ誤差",
  mean_signed_glideslope_error: "平均誤差（+ = 高い）",
  mean_glideslope_deviation: "平均偏差",
  mean_signed_glideslope_deviation: "平均偏差（+ = 高い）",
  mean_path_angle: "飛んだ経路角",
  crosswind_crab: "クラブ角（接地ヘディング − 対地トラック）",
  path_angle_spread: "直線からの浮き沈み",
  aim_offset: "狙点のずれ（+ = 接地点より手前）",
  glideslope: "基準スロープ",
  samples: "サンプル数",
  method: "測定方法",
  reference: "基準",
  max_abs_deviation: "最大横ずれ",
  max_centerline_deviation: "最大横ずれ",
  window: "評価窓",
  overshoot: "センターライン突き抜け",
  centerline_overshoot: "センターライン突き抜け",
  glideslope_reference: "基準",
  glideslope_method: "測定方法",
  outcome: "結果",
  approach_pattern: "進入パターン",
  approach_pattern_detected: "進入パターン（検出時の暫定判定）",
  unscored_reason: "採点しなかった理由",
  measured_components: "採点した項目",
  unmeasured_components: "未評価の項目",
  measured_weight: "採点できた重み",
  min_measured_weight: "成績を出す最低ライン",
  graded: "成績を付けたか",
  rollout_before_touchdown: "旋回明け（接地前）",
  // オーバーヘッドパターン
  rollout_offset: "旋回明けの軸ずれ（+ = 手前 / - = 突き抜け）",
  alignment_error: "旋回明けの軸ずれ量",
  downwind_course_error: "ダウンウィンド方位差",
  downwind_course_offset: "ダウンウィンド方位差（+ = 外へ広がる）",
  downwind_course_rms: "ダウンウィンドの直線からのばらつき",
  pattern_downwind_course_offset: "ダウンウィンド方位差（+ = 外へ広がる）",
  pattern_downwind_course_rms: "ダウンウィンドの直線からのばらつき",
  final_window: "採点したファイナルの長さ",
  final_start_anchor: "ファイナル起点の決まり方",
  pattern_rollout_time: "旋回明け時刻",
  pattern_downwind_start_time: "ダウンウィンド開始時刻",
  pattern_downwind_end_time: "ダウンウィンド終了時刻",
  downwind_altitude_spread: "ダウンウィンド高度変動",
  downwind_abeam: "ダウンウィンド離隔",
  downwind_duration: "ダウンウィンド長さ",
  downwind_samples: "ダウンウィンドのサンプル数",
  downwind_judged: "ダウンウィンドを採点したか",
  break_altitude_spread: "ブレイク中の高度変動",
  break_duration: "ブレイクの長さ",
  break_samples: "ブレイクのサンプル数",
  break_judged: "ブレイクを採点したか",
  pattern_break_altitude_spread: "ブレイク中の高度変動",
  pattern_break_duration: "ブレイクの長さ",
  pattern_break_samples: "ブレイクのサンプル数",
  pattern_break_judged: "ブレイクを採点したか",
  sub_scores: "内訳スコア",
  pattern_rollout_offset: "旋回明けの軸ずれ（+ = 手前 / - = 突き抜け）",
  pattern_alignment_error: "旋回明けの軸ずれ量",
  pattern_downwind_course_error: "ダウンウィンド方位差",
  pattern_downwind_altitude_spread: "ダウンウィンド高度変動",
  pattern_downwind_abeam: "ダウンウィンド離隔",
  pattern_downwind_duration: "ダウンウィンド長さ",
  pattern_downwind_samples: "ダウンウィンドのサンプル数",
  pattern_downwind_judged: "ダウンウィンドを採点したか",
  pattern_overshoot: "センターライン突き抜け",
};

/** Keys that exist for the charts, not for the reader.
 *
 * The plan view needs the leg boundaries as raw mission seconds to colour
 * the track; printing "19219.73" in a data sheet just adds noise, and the
 * same information is already there as 旋回明け（接地前）.
 */
const INTERNAL_METRIC_KEYS = new Set([
  "rollout_time",
  "downwind_start_time",
  "downwind_end_time",
  "pattern_rollout_time",
  "pattern_downwind_start_time",
  "pattern_downwind_end_time",
  "break_start_time",
  "break_end_time",
  "pattern_break_start_time",
  "pattern_break_end_time",
]);

export function isInternalMetricKey(key: string): boolean {
  return INTERNAL_METRIC_KEYS.has(key);
}

/** Full key first, then the unit-stripped stem, then the raw stem. */
function metricLabel(key: string, stem: string): string {
  return METRIC_LABELS[key] ?? METRIC_LABELS[stem] ?? stem;
}

export function factorDescription(name: string): string {
  return FACTOR_DESCRIPTIONS[name] ?? "";
}

/** 採点コンポーネントの短い日本語名（一覧に並べる用）。 */
const FACTOR_LABELS: Record<string, string> = {
  descent_rate: "接地降下率",
  touchdown_speed: "接地速度",
  glideslope: "グライドスロープ",
  centerline: "センターライン保持",
  pattern: "オーバーヘッドパターン",
};

export function factorLabel(name: string): string {
  return FACTOR_LABELS[name] ?? name;
}

/** Short label shown in the summary header. */
export function pilotLabel(item: Pick<LandingSummary, "pilot">): string {
  return item.pilot ?? "不明なプレイヤー";
}
