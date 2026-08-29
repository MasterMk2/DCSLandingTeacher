import { describe, expect, it } from "vitest";
import { applyLandingMessage, isProvisional, matchesFilters } from "./landings";
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

  it("only shows a new landing that matches the active filters (Issue #33)", () => {
    const res = response([summary({ id: 2, venue_name: "CV-59" })]);
    const next = applyLandingMessage(
      res,
      { type: "landing", landing: summary({ id: 3, venue_name: "Stennis" }) },
      0,
      { venue: "CV-59" },
    );
    // Filtered out: not inserted, but the unseen total still bumps.
    expect(next.items.map((it) => it.id)).toEqual([2]);
    expect(next.total).toBe(2);
  });

  it("inserts a new landing when it matches the active filters", () => {
    const res = response([summary({ id: 2, venue_name: "CV-59" })]);
    const next = applyLandingMessage(
      res,
      { type: "landing", landing: summary({ id: 3, venue_name: "CV-59" }) },
      0,
      { venue: "CV-59" },
    );
    expect(next.items.map((it) => it.id)).toEqual([3, 2]);
    expect(next.total).toBe(2);
  });
});

describe("matchesFilters", () => {
  it("matches on a case-insensitive substring", () => {
    const row = summary({ venue_name: "CV-59", kind: "carrier" });
    expect(matchesFilters(row, { venue: "cv" })).toBe(true);
    expect(matchesFilters(row, { venue: "Stennis" })).toBe(false);
    expect(matchesFilters(row, { kind: "CARRIER" })).toBe(true);
  });

  it("treats an unfiltered dimension as a match", () => {
    expect(matchesFilters(summary(), {})).toBe(true);
    expect(matchesFilters(summary(), { player: "" })).toBe(true);
  });

  it("rejects a row missing the filtered field", () => {
    expect(matchesFilters(summary({ venue_name: null }), { venue: "CV" })).toBe(false);
  });
});

describe("isProvisional", () => {
  it("treats missing status as final (backward compatibility)", () => {
    expect(isProvisional(summary())).toBe(false);
    expect(isProvisional(summary({ outcome_status: undefined }))).toBe(false);
    expect(isProvisional(summary({ outcome_status: "provisional" }))).toBe(true);
  });
});

describe("applyLandingMessage / live payloads", () => {
  const base = {
    items: [],
    total: 0,
    limit: 50,
    offset: 0,
  } as unknown as import("../types/api").LandingListResponse;

  it("inserts a live landing even when the payload is partial", () => {
    const msg = {
      type: "landing",
      landing: { id: 9, grade: "B", source_id: "default" },
    } as unknown as import("../types/api").WsLandingMessage;
    const next = applyLandingMessage(base, msg, 0);
    expect(next.items).toHaveLength(1);
    expect(next.total).toBe(1);
  });

  it("keeps uploaded recordings out of the shared history", () => {
    // The list endpoint hides them; the live feed must agree, or an import
    // drops rows into every dashboard that vanish on the next refetch.
    const msg = {
      type: "landing",
      landing: { id: 10, grade: "A", source_id: "import:abc123" },
    } as unknown as import("../types/api").WsLandingMessage;
    expect(applyLandingMessage(base, msg, 0).items).toHaveLength(0);
    // ...unless that source is the one being looked at.
    expect(
      applyLandingMessage(base, msg, 0, "import:abc123").items,
    ).toHaveLength(1);
  });
});
