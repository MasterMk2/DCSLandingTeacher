import { describe, expect, it } from "vitest";
import { applyLandingMessage, isProvisional } from "./landings";
import type { LandingListResponse, LandingSummary } from "../types/api";

function summary(overrides: Partial<LandingSummary> = {}): LandingSummary {
  return {
    id: 1,
    flight_id: 10,
    kind: "carrier",
    outcome: "full_stop",
    outcome_status: "final",
    venue_name: "CV-59",
    pilot: "Viggen",
    airframe: "F/A-18C",
    touchdown_time: 1000,
    grade: "OK",
    score: null,
    created_at: "2024-01-01T00:00:00Z",
    ...overrides,
  };
}

function response(items: LandingSummary[]): LandingListResponse {
  return { items, total: items.length, limit: 50, offset: 0 };
}

describe("applyLandingMessage", () => {
  it("inserts a new provisional landing on the first page", () => {
    const res = response([summary({ id: 2 })]);
    const next = applyLandingMessage(
      res,
      { type: "landing", landing: summary({ id: 3, outcome_status: "provisional" }) },
      0,
    );
    expect(next.items.map((it) => it.id)).toEqual([3, 2]);
    expect(next.total).toBe(2);
    expect(isProvisional(next.items[0])).toBe(true);
  });

  it("only bumps total for new landings on deeper pages", () => {
    const res = response([summary({ id: 2 })]);
    const next = applyLandingMessage(
      res,
      { type: "landing", landing: summary({ id: 3 }) },
      50,
    );
    expect(next.items).toHaveLength(1);
    expect(next.total).toBe(2);
  });

  it("replaces a provisional row when the final update arrives", () => {
    const res = response([
      summary({ id: 3, outcome_status: "provisional", grade: "-" }),
    ]);
    const next = applyLandingMessage(
      res,
      {
        type: "landing_update",
        landing: summary({ id: 3, outcome_status: "final", grade: "OK-" }),
      },
      0,
    );
    expect(next.items).toHaveLength(1);
    expect(next.total).toBe(1);
    expect(next.items[0].grade).toBe("OK-");
    expect(next.items[0].outcome_status).toBe("final");
    expect(isProvisional(next.items[0])).toBe(false);
  });

  it("merges a duplicate landing notification into the existing row", () => {
    const res = response([summary({ id: 3, outcome_status: "provisional" })]);
    const next = applyLandingMessage(
      res,
      { type: "landing", landing: summary({ id: 3, outcome_status: "provisional" }) },
      0,
    );
    expect(next.items).toHaveLength(1);
    expect(next.total).toBe(1);
  });

  it("ignores updates for rows that are not on the current page", () => {
    const res = response([summary({ id: 2 })]);
    const next = applyLandingMessage(
      res,
      { type: "landing_update", landing: summary({ id: 99, grade: "CUT" }) },
      0,
    );
    expect(next).toBe(res);
  });

  it("ignores messages without an id", () => {
    const res = response([summary()]);
    const next = applyLandingMessage(res, { type: "landing", landing: {} }, 0);
    expect(next).toBe(res);
  });
});

describe("isProvisional", () => {
  it("treats missing status as final (backward compatibility)", () => {
    expect(isProvisional(summary())).toBe(false);
    expect(isProvisional(summary({ outcome_status: undefined }))).toBe(false);
    expect(isProvisional(summary({ outcome_status: "provisional" }))).toBe(true);
  });
});
