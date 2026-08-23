import { describe, expect, it } from "vitest";
import {
  factorDescription,
  gradeClass,
  kindLabel,
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
