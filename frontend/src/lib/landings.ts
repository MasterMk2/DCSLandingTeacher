/**
 * Pure reducers that fold real-time WebSocket messages into the landing
 * list response (Issue #5 two-phase confirmation).
 *
 * - "landing":        insert a newly detected landing (may be provisional);
 *                     if the id already exists, merge the payload instead.
 * - "landing_update": merge the confirmed payload into the existing row.
 */
import type {
  LandingFilters,
  LandingListResponse,
  LandingSummary,
  WsLandingMessage,
} from "../types/api";

function mergeItem(existing: LandingSummary, patch: Partial<LandingSummary>): LandingSummary {
  return { ...existing, ...patch } as LandingSummary;
}

/**
 * Whether a landing row satisfies the active list filters. Used to keep live
 * WS inserts from appearing in a filtered view they don't belong to (Issue
 * #33): a landing that the server would exclude on the next refetch must not
 * be shown as a live row in the meantime.
 */
export function matchesFilters(landing: LandingSummary, filters: LandingFilters): boolean {
  const checks: Array<[string | null | undefined, string | undefined]> = [
    [landing.pilot, filters.player],
    [landing.airframe, filters.airframe],
    [landing.venue_name, filters.venue],
    [landing.kind, filters.kind],
    [landing.grade, filters.grade],
    [landing.outcome, filters.outcome],
  ];
  for (const [value, wanted] of checks) {
    if (wanted === undefined || wanted === "") continue;
    if (value === null || value === undefined) return false;
    if (!value.toLowerCase().includes(wanted.toLowerCase())) return false;
  }
  return true;
}

export function applyLandingMessage(
  res: LandingListResponse,
  msg: WsLandingMessage,
  offset: number,
  filters?: LandingFilters,
): LandingListResponse {
  const landing = msg.landing;
  if (landing.id === undefined) return res;

  const index = res.items.findIndex((it) => it.id === landing.id);

  if (msg.type === "landing_update" || index !== -1) {
    // Confirmation of a provisional row (or a duplicate notification):
    // update in place without touching totals.
    if (index === -1) return res;
    const items = [...res.items];
    items[index] = mergeItem(items[index], landing);
    return { ...res, items };
  }

  // Brand-new landing: prepend on the first page when it matches the active
  // filters; deeper pages (or filtered-out rows) only bump the total so the
  // unseen badge stays correct without polluting the displayed list.
  const matched = !filters || matchesFilters(landing as LandingSummary, filters);
  if (offset === 0 && matched) {
    return {
      ...res,
      items: [landing as LandingSummary, ...res.items],
      total: res.total + 1,
    };
  }
  return { ...res, total: res.total + 1 };
}

/** True when the row still awaits its final outcome ("評価中" badge). */
export function isProvisional(item: LandingSummary): boolean {
  return item.outcome_status === "provisional";
}
