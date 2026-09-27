import { describe, expect, it } from "vitest";
import { airframeCaption, airframeModel, allAirframeModels, type AirframeModel } from "./airframes";

describe("airframeModel", () => {
  it("recognises DCS type names as the recordings carry them", () => {
    const cases: [string, string][] = [
      ["FA-18C_hornet", "F/A-18C"],
      ["F/A-18C", "F/A-18C"],
      ["F-14B", "F-14"],
      ["F-14A-135-GR", "F-14"],
      ["Su-33", "Su-33"],
      ["F-16C_50", "F-16C"],
      ["F-15ESE", "F-15E"],
      ["F-15C", "F-15C"],
      ["M-2000C", "Mirage 2000"],
      ["AV8BNA", "AV-8B"],
      ["A-10C_2", "A-10"],
      ["AJS37", "AJS 37"],
      ["JF-17", "JF-17"],
      ["MiG-29S", "MiG-29"],
      ["MiG-21Bis", "MiG-21"],
      ["F-4E-45MC", "F-4E"],
      ["F-5E-3", "F-5E"],
      ["F-86F Sabre", "F-86F"],
      ["MiG-15bis", "MiG-15"],
      ["L-39ZA", "L-39"],
      ["C-101CC", "C-101"],
      ["MB-339A", "MB-339"],
      ["Mirage-F1CE", "Mirage F1"],
      ["A-4E-C", "A-4E"],
      ["TF-51D", "P-51D"],
      ["SpitfireLFMkIX", "Spitfire"],
      ["SpitfireLFMkIXCW", "Spitfire LF Mk IX CW"],
      ["Bf-109K-4", "Bf 109"],
      ["FW-190D9", "Fw 190D"],
      ["E-2C", "E-2"],
      ["S-3B Tanker", "S-3B"],
      ["KC130", "C-130"],
      ["KC135MPRS", "KC-135"],
      ["E-3A", "E-3"],
      ["A-50", "A-50"],
      ["Tu-22M3", "Tu-22M3"],
      ["UH-1H", "UH-1H"],
      ["AH-64D_BLK_II", "AH-64D"],
      ["Ka-50_3", "Ka-50"],
      ["Mi-8MT", "Mi-8"],
      ["Mi-24P", "Mi-24"],
      ["SA342M", "SA 342"],
      ["OH58D", "OH-58D"],
      ["CH-47Fbl1", "CH-47"],
      ["UH-60A", "UH-60"],
    ];
    for (const [name, label] of cases) expect(airframeModel(name).label, name).toBe(label);
  });

  it("matches from the start of the name, the longest prefix winning", () => {
    expect(airframeModel("FA-18E").label).toBe("F/A-18E/F");
    // "TF51D" contains "F5" but is no F-5.
    expect(airframeModel("TF-51D").label).toBe("P-51D");
  });

  it("falls back to the generic jet, and the caption says why", () => {
    const unknown = airframeModel("F-35C");
    expect(unknown.label).toBeNull();
    expect(airframeCaption("F-35C", unknown)).toContain("「F-35C」の形は未登録");
    expect(airframeModel(null).label).toBeNull();
    expect(airframeModel("").label).toBeNull();
    expect(airframeCaption(null, airframeModel(null))).toContain("機種が記録に無い");
  });

  it("says when an outline is borrowed from another aircraft", () => {
    expect(airframeCaption("MiG-27K", airframeModel("MiG-27K"))).toContain("Tornado の形");
    expect(airframeCaption("FA-18C_hornet", airframeModel("FA-18C_hornet"))).not.toContain("代用");
    // Options that draw the aircraft's own layout are not a borrowed shape.
    expect(airframeCaption("IL-76MD", airframeModel("IL-76MD"))).not.toContain("代用");
    expect(airframeCaption("UH-1H", airframeModel("UH-1H"))).toContain("ローター直径 14.6 m");
  });
});

function extent(model: AirframeModel, axis: 0 | 1 | 2): [number, number] {
  let min = Infinity;
  let max = -Infinity;
  for (let i = axis; i < model.positions.length; i += 3) {
    min = Math.min(min, model.positions[i]);
    max = Math.max(max, model.positions[i]);
  }
  return [min, max];
}

describe("the outlines", () => {
  const models = allAirframeModels();

  it("come as unit-length triangles centred on their length", () => {
    for (const m of models) {
      const name = m.label ?? "generic";
      expect(m.positions.length % 9, name).toBe(0);
      expect(m.colors.length, name).toBe(m.positions.length);
      expect(m.positions.every(Number.isFinite), name).toBe(true);
      const [min, max] = extent(m, 0);
      expect(min, name).toBeCloseTo(-0.5, 6);
      expect(max, name).toBeCloseTo(0.5, 6);
    }
  });

  it("have their glass forward and on top, so nose and up read right", () => {
    for (const m of models) {
      let x = 0;
      let y = 0;
      let n = 0;
      for (let i = 0; i < m.colors.length; i += 3) {
        if (m.colors[i] < 0.3) {
          x += m.positions[i];
          y += m.positions[i + 1];
          n++;
        }
      }
      const name = m.label ?? "generic";
      expect(n, name).toBeGreaterThan(0);
      expect(x / n, name).toBeGreaterThan(0);
      expect(y / n, name).toBeGreaterThan(0);
    }
  });

  it("are drawn to the aircraft's real length and span", () => {
    for (const m of models.filter((model) => !model.rotorcraft)) {
      const name = m.label ?? "generic";
      expect(Math.abs(m.lengthM / m.specM.length - 1), name).toBeLessThan(0.05);
      expect(Math.abs(m.spanM / m.specM.span - 1), name).toBeLessThan(0.03);
      // Left and right alike: a surface drawn on one side only shows here.
      const [minZ, maxZ] = extent(m, 2);
      expect(maxZ + minZ, name).toBeCloseTo(0, 6);
    }
    for (const m of models.filter((model) => model.rotorcraft)) {
      // The fuselage fits in the drawn length; the blades within the disc.
      expect(m.lengthM, m.label!).toBeGreaterThanOrEqual(m.specM.length);
      expect(m.spanM, m.label!).toBeLessThanOrEqual(m.specM.span + 1e-6);
    }
  });
});
