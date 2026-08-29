/** Dashboard data hook: paged fetch + real-time WS insertion. */

import { useCallback, useEffect, useRef, useState } from "react";
import { listLandings } from "../api/client";
import { LandingSocket } from "../api/ws";
import { applyLandingMessage, countsAsUnseen } from "../lib/landings";
import type {
  LandingFilters,
  LandingListResponse,
} from "../types/api";

export interface UseLandingsResult {
  data: LandingListResponse | null;
  loading: boolean;
  error: string | null;
  /** Number of live-inserted rows not yet confirmed by a refetch. */
  liveCount: number;
  refresh: () => void;
}

const PAGE_SIZE = 50;

export function useLandings(
  filters: LandingFilters,
  offset: number,
): UseLandingsResult {
  const [data, setData] = useState<LandingListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [liveCount, setLiveCount] = useState(0);
  const [reloadTick, setReloadTick] = useState(0);

  // Stable JSON key so the effect does not re-run on every render.
  const filterKey = JSON.stringify(filters);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listLandings(JSON.parse(filterKey) as LandingFilters, PAGE_SIZE, offset)
      .then((res) => {
        if (!cancelled) {
          setData(res);
          setError(null);
          setLiveCount(0);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [filterKey, offset, reloadTick]);

  useEffect(() => {
    const socket = new LandingSocket((msg) => {
      // "landing" inserts a new (possibly provisional) row; "landing_update"
      // replaces the provisional row with its confirmed outcome (Issue #5).
      // Capturing filters via filtersRef (not the effect closure) keeps the
      // live insert consistent with the currently displayed filter set
      // (Issue #33), so a filtered-out landing is counted but not shown.
      // The whole filter set is handed over rather than just `source`: the
      // reducer needs `source` to keep uploaded recordings out of the shared
      // history, and the remaining fields for the Issue #33 check.
      //
      // The badge is decided by countsAsUnseen against the same state and the
      // same filters, so it can never claim news the reducer discarded (an
      // import's landings, a replayed frame after a reconnect, a row the
      // filters exclude) -- clicking it would refetch and reveal nothing.
      setData((prev) => {
        if (!prev) return prev;
        if (countsAsUnseen(prev, msg, filtersRef.current)) {
          setLiveCount((c) => c + 1);
        }
        return applyLandingMessage(prev, msg, offset, filtersRef.current);
      });
    });
    socket.connect();
    return () => socket.close();
  }, [offset]);

  const refresh = useCallback(() => setReloadTick((t) => t + 1), []);

  return { data, loading, error, liveCount, refresh };
}

export { PAGE_SIZE as LANDING_PAGE_SIZE };
