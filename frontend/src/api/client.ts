/**
 * Typed fetch-based REST client for the landing API.
 * In dev, requests go through the Vite proxy (/api -> backend).
 */
import type {
  LandingDetail,
  LandingFilters,
  LandingListResponse,
  LandingSummary,
} from "../types/api";

const BASE = import.meta.env.VITE_API_BASE ?? "";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    throw new ApiError(res.status, `GET ${path} failed: ${res.status}`);
  }
  return (await res.json()) as T;
}

function buildQuery(
  filters: LandingFilters,
  limit?: number,
  offset?: number,
): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") params.set(key, value);
  }
  if (limit !== undefined) params.set("limit", String(limit));
  if (offset !== undefined) params.set("offset", String(offset));
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export function listLandings(
  filters: LandingFilters = {},
  limit = 50,
  offset = 0,
): Promise<LandingListResponse> {
  return getJson<LandingListResponse>(
    `/api/landings${buildQuery(filters, limit, offset)}`,
  );
}

export function getLanding(id: number): Promise<LandingDetail> {
  return getJson<LandingDetail>(`/api/landings/${id}`);
}

/** Fetch every page matching the filters (for CSV export). */
export async function listAllLandings(
  filters: LandingFilters = {},
): Promise<LandingSummary[]> {
  const pageSize = 200;
  let offset = 0;
  const all: LandingListResponse["items"] = [];
  for (;;) {
    const page = await listLandings(filters, pageSize, offset);
    all.push(...page.items);
    if (all.length >= page.total || page.items.length === 0) break;
    offset += page.items.length;
  }
  return all;
}
