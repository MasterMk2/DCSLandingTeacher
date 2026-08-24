/** Dashboard data hook: paged fetch + real-time WS insertion. */

import { useCallback, useEffect, useRef, useState } from "react";
import { listLandings } from "../api/client";
import { LandingSocket } from "../api/ws";
import { applyLandingMessage } from "../lib/landings";
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
      const isNew = msg.type === "landing";
      setData((prev) =>
        prev ? applyLandingMessage(prev, msg, offset) : prev,
      );
      if (isNew) setLiveCount((c) => c + 1);
    });
    socket.connect();
    return () => socket.close();
  }, [offset]);

  const refresh = useCallback(() => setReloadTick((t) => t + 1), []);

  return { data, loading, error, liveCount, refresh };
}

export { PAGE_SIZE as LANDING_PAGE_SIZE };
