/**
 * Pure reducers that fold real-time WebSocket messages into the landing
 * list response (Issue #5 two-phase confirmation).
 *
 * - "landing":        insert a newly detected landing (may be provisional);
 *                     if the id already exists, merge the payload instead.
 * - "landing_update": merge the confirmed payload into the existing row.
 */
import type {
  LandingListResponse,
  LandingSummary,
  WsLandingMessage,
} from "../types/api";

function mergeItem(existing: LandingSummary, patch: Partial<LandingSummary>): LandingSummary {
  return { ...existing, ...patch } as LandingSummary;
}

/** Uploaded recordings are scratch data scoped to their own source. */
export const IMPORT_SOURCE_PREFIX = "import:";

export function applyLandingMessage(
  res: LandingListResponse,
  msg: WsLandingMessage,
  offset: number,
  activeSource?: string | null,
): LandingListResponse {
  const landing = msg.landing;
  if (landing.id === undefined) return res;

  // The list endpoint hides uploaded recordings from the shared history, so
  // the live feed must hide them too -- otherwise an import drops rows into
  // everyone's dashboard that vanish again on the next refetch.
  const source = landing.source_id;
  if (
    typeof source === "string" &&
    source.startsWith(IMPORT_SOURCE_PREFIX) &&
    activeSource !== source
  ) {
    return res;
  }

  const index = res.items.findIndex((it) => it.id === landing.id);

  if (msg.type === "landing_update" || index !== -1) {
    // Confirmation of a provisional row (or a duplicate notification):
    // update in place without touching totals.
    if (index === -1) return res;
    const items = [...res.items];
    items[index] = mergeItem(items[index], landing);
    return { ...res, items };
  }

  // Brand-new landing: prepend on the first page; deeper pages only bump
  // the total so the badge count reflects unseen rows.
  if (offset === 0) {
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
