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

  it("summarises the carrier geometry instead of dumping its JSON", () => {
    // 甲板と一緒に動く座標で採点した記録 (deviations.py の payload そのまま)。
    expect(
      formatMetric("flols_geometry", {
        key: "nimitz_supercarrier",
        source: "carriers.yaml",
        validated: false,
        deck_altitude_m: 20.15,
        ramp_along_m: -162.49,
        ramp_lateral_m: 9.38,
        glideslope_deg: 3.5,
        landing_course_offset_deg: -9.1359,
        touchdown_target_m: 79.0,
        reference_height_m: 2.0,
        landing_area_length_m: 250.0,
        beam_width_m: 12.0,
        frame: "moving_deck",
        ship_heading_deg: 87.5,
        ship_altitude_m: 0.0,
        ship_speed_ms: 15.2,
        ship_heading_change_deg: 0.4,
      }),
    ).toEqual({
      label: "採点に使った着艦幾何",
      text:
        "nimitz_supercarrier（グライドスロープ 3.5°、アングルドデッキ 左 9.1°、" +
        "甲板高 66 ft、狙点 ランプから 259 ft）",
    });
    // 狙点の無い旧 entry はグライドスロープをランプで終えていた。
    expect(
      formatMetric("flols_geometry", {
        key: "kuznetsov",
        glideslope_deg: 4.0,
        landing_course_offset_deg: -2.0,
        deck_altitude_m: 22.0,
        touchdown_target_m: 0.0,
      }).text,
    ).toBe("kuznetsov（グライドスロープ 4.0°、アングルドデッキ 左 2.0°、甲板高 72 ft、狙点 ランプ）");
    expect(
      formatMetric("flols_geometry", { source: "touchdown_reference_fallback" }).text,
    ).toBe("未登録（接地点を基準に近似）");
  });

  it("uses the ramp for persisted carrier geometry without a touchdown target", () => {
    // touchdown_target_m が保存されていない旧データも backend と同じ 0 として扱う。
    expect(
      formatMetric("flols_geometry", {
        key: "kuznetsov",
        glideslope_deg: 4.0,
        landing_course_offset_deg: -2.0,
        deck_altitude_m: 22.0,
      }),
    ).toEqual({
      label: "採点に使った着艦幾何",
      text: "kuznetsov（グライドスロープ 4.0°、アングルドデッキ 左 2.0°、甲板高 72 ft、狙点 ランプ）",
    });
  });

  it.each([null, "79", Number.NaN, Number.POSITIVE_INFINITY])(
    "does not infer a ramp target from an explicitly invalid value: %s",
    (touchdown_target_m) => {
      expect(
        formatMetric("flols_geometry", { key: "kuznetsov", touchdown_target_m }).text,
      ).toBe("kuznetsov");
    },
  );

  it("labels the break kinematics and gives the load factor its unit", () => {
    // 無次元の荷重倍数はキーに接尾辞が無い。"G" を付けないと「2.31」が
    // 何の数字か分からない。
    expect(formatMetric("pattern_break_max_load_factor", 2.312)).toEqual({
      label: "ブレイク最大 G",
      text: "2.31 G",
    });
    expect(formatMetric("break_load_factor_std", 0.084).text).toBe("0.08 G");
    // 旋回率は "_s" で終わるが秒ではない。
    expect(formatMetric("pattern_break_mean_turn_rate_deg_s", 8.66)).toEqual({
      label: "ブレイク平均旋回率",
      text: "8.7°/s",
    });
    expect(formatMetric("pattern_break_mean_turn_rate_deg_s", null).label).toBe(
      "ブレイク平均旋回率",
    );
    expect(formatMetric("pattern_break_start_along_m", -926).text).toBe("-0.50 nm");
    expect(formatMetric("pattern_break_entry_speed_ms", 154.3).text).toBe("300 kt");
    expect(formatMetric("pattern_break_max_bank_deg", 63.4).label).toBe(
      "ブレイク最大バンク角（記録の Roll）",
    );
    expect(formatMetric("pattern_break_sustained_s", 6.4).text).toBe("6.4 s");
    // 1% ルール: 目標は G、比は倍率。
    expect(formatMetric("pattern_break_one_percent_rule_g", 3.03)).toEqual({
      label: "1% ルールの目標 G（進入速度 kt ÷ 100、経験則）",
      text: "3.03 G",
    });
    expect(formatMetric("pattern_break_max_g_to_one_percent", 0.96).text).toBe("0.96 倍");
    expect(formatMetric("kinematics_rejected_samples", 7)).toEqual({
      label: "位置の飛びとして G の算出から外したサンプル数",
      text: "7",
    });
  });

  it("reads the Case I checkpoints in the units the pattern is flown in", () => {
    // Distances in nm, heights in ft -- including the abeam HEIGHT, whose key
    // contains "abeam" like the distance does.
    expect(formatMetric("pattern_abeam_distance_m", 2083.8)).toEqual({
      label: "アビーム距離（艦の中心線から）",
      text: "1.13 nm",
    });
    expect(formatMetric("pattern_abeam_altitude_m", 182.9)).toEqual({
      label: "アビーム高度（MSL）",
      text: "600 ft",
    });
    expect(formatMetric("pattern_break_along_ship_m", 1240.8).text).toBe("0.67 nm");
    expect(formatMetric("pattern_groove_start_distance_m", 894.5).text).toBe("0.48 nm");
    expect(formatMetric("pattern_groove_start_lineup_m", -3.0).text).toBe("-10 ft");
    expect(formatMetric("pattern_groove_time_s", 15.6).text).toBe("15.6 s");
    expect(formatMetric("pattern_groove_verdict", "LIG").text).toBe("LIG（長い: 19 秒超）");
    expect(formatMetric("pattern_ship_speed_ms", 15.0).text).toBe("29 kt");
    expect(formatMetric("ramp_descent_rate_fpm", 207).label).toBe("ランプ直前 1 秒の降下率");
    expect(formatMetric("deck_frame", "moving_deck").text).toContain("甲板と一緒に動く");
    expect(formatMetric("airframe_class", "carrier").text).toContain("艦載機");
  });
});
