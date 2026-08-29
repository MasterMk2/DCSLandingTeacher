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

/** Uploaded recordings are scratch data scoped to their own source. */
export const IMPORT_SOURCE_PREFIX = "import:";

/**
 * Whether a landing row satisfies the active list filters. Used to keep live
 * WS inserts from appearing in a filtered view they don't belong to (Issue
 * #33): a landing that the server would exclude on the next refetch must not
 * be shown as a live row in the meantime.
 *
 * The predicates mirror `list_landings` in backend/app/api/routes.py one for
 * one, because every divergence surfaces as a row that pops into the list and
 * silently disappears on the next refetch (or the reverse):
 *
 * - player / airframe / venue use `ILIKE '%x%'`  -> case-insensitive substring
 * - kind / grade / outcome / pattern / source use `=`  -> whole-value match.
 *   Substring matching here would be wrong, not merely loose: a `grade=OK`
 *   filter would keep `OK-` and `(OK)` rows the server never returns.
 * - date_from / date_to bracket `created_at`
 *
 * A row that lacks the filtered field cannot match, the same way NULL never
 * satisfies ILIKE, `=` or a range comparison in SQL.
 */
export function matchesFilters(landing: LandingSummary, filters: LandingFilters): boolean {
  const contains: Array<[string | null | undefined, string | undefined]> = [
    [landing.pilot, filters.player],
    [landing.airframe, filters.airframe],
    [landing.venue_name, filters.venue],
  ];
  for (const [value, wanted] of contains) {
    if (!wanted) continue;
    if (typeof value !== "string") return false;
    if (!value.toLowerCase().includes(wanted.toLowerCase())) return false;
  }

  const equals: Array<[string | null | undefined, string | undefined]> = [
    [landing.kind, filters.kind],
    [landing.grade, filters.grade],
    [landing.outcome, filters.outcome],
    [landing.approach_pattern, filters.pattern],
    [landing.source_id, filters.source],
  ];
  for (const [value, wanted] of equals) {
    if (!wanted) continue;
    if (typeof value !== "string") return false;
    // Case is folded so a hand-written filter still behaves; every value the
    // UI can emit comes from a fixed dropdown that already carries the
    // server's exact spelling, so in practice this is plain equality.
    if (value.toLowerCase() !== wanted.toLowerCase()) return false;
  }

  // `created_at` and the datetime-local filter values are both naive ISO-8601
  // strings, so comparing them lexicographically reproduces the server's naive
  // comparison. Parsing them into Date would be worse: the backend value is
  // naive UTC while a datetime-local value is local wall clock, and Date would
  // interpret the two in different zones.
  const recordedAt = landing.created_at;
  if (filters.date_from || filters.date_to) {
    if (typeof recordedAt !== "string") return false;
    if (filters.date_from && recordedAt < filters.date_from) return false;
    if (filters.date_to && recordedAt > filters.date_to) return false;
  }

  return true;
}

export function applyLandingMessage(
  res: LandingListResponse,
  msg: WsLandingMessage,
  offset: number,
  filters?: LandingFilters | string | null,
): LandingListResponse {
  const landing = msg.landing;
  if (landing.id === undefined) return res;

  // A bare string is the active source id -- the shape callers used before the
  // filter-aware insert of Issue #33 -- and is still accepted so a caller that
  // only knows which source is on screen keeps working.
  const active: LandingFilters =
    typeof filters === "string" ? { source: filters } : (filters ?? {});

  // The list endpoint hides uploaded recordings from the shared history, so
  // the live feed must hide them too -- otherwise an import drops rows into
  // everyone's dashboard that vanish again on the next refetch. This is
  // checked before the Issue #33 filter test and regardless of it: such a row
  // is not merely filtered out of the current view, it is not part of the
  // shared list at all, so it must not bump the unseen counter either.
  const source = landing.source_id;
  if (
    typeof source === "string" &&
    source.startsWith(IMPORT_SOURCE_PREFIX) &&
    active.source !== source
  ) {
    return res;
  }

  const index = res.items.findIndex((it) => it.id === landing.id);

  if (msg.type === "landing_update" || index !== -1) {
    // Confirmation of a provisional row (or a duplicate notification):
    // update in place without touching totals. A displayed row keeps being
    // updated even if the new payload no longer matches the filters; the next
    // refetch is what removes it.
    if (index === -1) return res;
    const items = [...res.items];
    items[index] = mergeItem(items[index], landing);
    return { ...res, items };
  }

  // Brand-new landing: prepend on the first page when it matches the active
  // filters; deeper pages (or filtered-out rows) only bump the total so the
  // unseen badge stays correct without polluting the displayed list.
  if (offset === 0 && matchesFilters(landing as LandingSummary, active)) {
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
