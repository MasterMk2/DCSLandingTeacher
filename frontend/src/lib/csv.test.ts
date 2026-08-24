import { describe, expect, it } from "vitest";
import { landingsToCsv, samplesToCsv, toCsv, withBom } from "./csv";

describe("toCsv", () => {
  it("joins headers and rows with CRLF", () => {
    const csv = toCsv(["a", "b"], [[1, "x"], [2, "y"]]);
    expect(csv).toBe("a,b\r\n1,x\r\n2,y");
  });

  it("quotes cells containing commas, quotes and newlines", () => {
    const csv = toCsv(["v"], [['has,comma'], ['has"quote'], ["line\nbreak"]]);
    expect(csv).toBe(
      'v\r\n"has,comma"\r\n"has""quote"\r\n"line\nbreak"',
    );
  });

  it("renders null/undefined as empty cells", () => {
    const csv = toCsv(["a", "b"], [[null, undefined]]);
    expect(csv).toBe("a,b\r\n,");
  });
});

describe("withBom", () => {
  it("prepends a UTF-8 BOM", () => {
    expect(withBom("abc").charCodeAt(0)).toBe(0xfeff);
    expect(withBom("abc").slice(1)).toBe("abc");
  });
});

describe("landingsToCsv", () => {
  it("emits one row per landing with empty cells for missing values", () => {
    const csv = landingsToCsv([
      {
        id: 7,
        kind: "carrier",
        outcome: "bolter",
        venue_name: "CVN-73",
        pilot: "テスト太郎",
        airframe: "F/A-18C",
        touchdown_time: 1700000000,
        touchdown_epoch: 1700000000,
        grade: "_NO_GRADE_",
        score: null,
      },
    ]);
    const lines = csv.split("\r\n");
    expect(lines[0]).toContain("id,kind,outcome");
    expect(lines[1]).toBe(
      "7,carrier,bolter,CVN-73,テスト太郎,F/A-18C,1700000000,1700000000,_NO_GRADE_,",
    );
  });
});

describe("samplesToCsv", () => {
  it("converts meters to nm/ft and m/s to knots (Issue D-4)", () => {
    const csv = samplesToCsv([
      {
        time: 100.5,
        distance_to_go: 3700.25,
        glideslope_deviation: -12.5,
        centerline_deviation: 3.2,
        speed: 72.4,
        aoa: null,
        agl: 195.1,
      },
    ]);
    const lines = csv.split("\r\n");
    // 3700.25 m -> 2.0 nm, -12.5 m -> -41.01 ft, 3.2 m -> 10.5 ft,
    // 72.4 m/s -> 140.73 kt, 195.1 m -> 640.09 ft.
    expect(lines[1]).toBe("100.5,2,-41.01,10.5,140.73,,640.09");
  });

  it("emits blanks for null deviation/altitude cells", () => {
    const csv = samplesToCsv([
      {
        time: 1.0,
        distance_to_go: 0,
        glideslope_deviation: null,
        centerline_deviation: null,
        speed: null,
        aoa: null,
        agl: null,
      },
    ]);
    const lines = csv.split("\r\n");
    expect(lines[1]).toBe("1,0,,,,,");
  });
});
