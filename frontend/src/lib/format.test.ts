import { describe, expect, it } from "vitest";
import {
  factorDescription,
  formatMetric,
  gradeClass,
  kindLabel,
  mToFt,
  mToNm,
  msToFpm,
  msToKnots,
  outcomeLabel,
} from "./format";

describe("unit conversions", () => {
  it("converts m/s to knots", () => {
    expect(msToKnots(0)).toBe(0);
    expect(msToKnots(51.4444)).toBeCloseTo(100, 1);
  });

  it("converts m/s to feet per minute", () => {
    expect(msToFpm(0)).toBe(0);
    expect(msToFpm(1)).toBeCloseTo(196.85, 1);
  });

  it("converts meters to feet (Issue D-4)", () => {
    expect(mToFt(0)).toBe(0);
    expect(mToFt(1)).toBeCloseTo(3.2808, 3);
    expect(mToFt(304.8)).toBeCloseTo(1000, 1);
  });

  it("converts meters to nautical miles (Issue D-4)", () => {
    expect(mToNm(0)).toBe(0);
    expect(mToNm(1852)).toBeCloseTo(1, 5);
    expect(mToNm(3704)).toBeCloseTo(2, 1);
  });
});

describe("labels", () => {
  it("maps kinds to Japanese labels", () => {
    expect(kindLabel("carrier")).toBe("空母着艦");
    expect(kindLabel("land")).toBe("陸上着陸");
    expect(kindLabel(null)).toBe("-");
  });

  it("maps outcomes to Japanese labels", () => {
    expect(outcomeLabel("full_stop")).toBe("フルストップ");
    expect(outcomeLabel("touch_and_go")).toBe("タッチアンドゴー");
    expect(outcomeLabel("bolter")).toBe("ボルター");
    expect(outcomeLabel(undefined)).toBe("-");
  });

  it("describes known factors and falls back to empty string", () => {
    expect(factorDescription("AOS")).not.toBe("");
    expect(factorDescription("UNKNOWN_X")).toBe("");
  });
});

describe("gradeClass", () => {
  it("classifies LSO grades", () => {
    expect(gradeClass("OK")).toBe("grade-ok");
    expect(gradeClass("OK-")).toBe("grade-ok-minus");
    expect(gradeClass("(OK)")).toBe("grade-paren-ok");
    expect(gradeClass("_NO_GRADE_")).toBe("grade-no-grade");
    expect(gradeClass("CUT")).toBe("grade-cut");
  });

  it("handles missing grades", () => {
    expect(gradeClass(null)).toBe("grade-none");
    expect(gradeClass("")).toBe("grade-none");
    expect(gradeClass("???")).toBe("grade-other");
  });
});

describe("formatMetric", () => {
  it("renders metre-suffixed deviations as feet and drops the stale suffix", () => {
    // Issue D-4: the UI is ft/kt/nm everywhere else; these read as metres.
    expect(formatMetric("max_abs_deviation_m", 28.01)).toEqual({
      label: "最大横ずれ",
      text: "92 ft",
    });
    expect(formatMetric("rms_deviation_final_15s_m", 16.91).text).toBe("55 ft");
  });

  it("distinguishes descent rates from airspeeds, both stored as m/s", () => {
    expect(formatMetric("touchdown_speed_ms", 89.86)).toEqual({
      label: "接地速度",
      text: "175 kt",
    });
    expect(formatMetric("recent_descent_ms", 4.0)).toEqual({
      label: "recent_descent",
      text: "787 fpm",
    });
  });

  it("passes through units that are already display-ready", () => {
    expect(formatMetric("touchdown_descent_rate_fpm", 456.6).text).toBe("457 fpm");
    expect(formatMetric("glideslope_deg", 3.0).text).toBe("3.0°");
    expect(formatMetric("window_s", 2.5).text).toBe("2.5 s");
  });

  it("leaves unitless and non-numeric values alone", () => {
    expect(formatMetric("speed_ratio", 0.867)).toEqual({
      label: "速度比",
      text: "0.87",
    });
    expect(formatMetric("major_factor_count", 2).text).toBe("2");
    expect(formatMetric("verdict", "hard").text).toBe("hard");
    expect(formatMetric("touchdown_speed_ms", null).text).toBe("-");
  });

  it("keeps the raw stem for keys nobody has labelled yet", () => {
    // 未知のキーで "undefined" を出さないこと: 採点側が新しい evidence を
    // 足したときに、UI 側の辞書更新が漏れても読める形で出る必要がある。
    expect(formatMetric("some_new_thing_m", 30.48).label).toBe("some_new_thing");
    expect(formatMetric("recent_descent_ms", 4.0).label).toBe("recent_descent");
  });

  it("labels the overhead pattern evidence in Japanese", () => {
    expect(formatMetric("downwind_course_error_deg", 9.4)).toEqual({
      label: "ダウンウィンド方位差",
      text: "9.4°",
    });
    // 離隔だけは nm。1.5 nm を 9000 ft と言われても飛ぶ側の感覚と合わない。
    expect(formatMetric("downwind_abeam_m", 2778).text).toBe("1.50 nm");
    expect(formatMetric("rollout_offset_m", -91.84).text).toBe("-301 ft");
  });

  it("renders nested evidence instead of [object Object]", () => {
    const { text } = formatMetric("sub_scores", { alignment: 98.6 });
    expect(text).toBe('{"alignment":98.6}');
  });
});
