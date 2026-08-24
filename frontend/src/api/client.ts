/**
 * Typed fetch-based REST client for the landing API.
 * In dev, requests go through the Vite proxy (/api -> backend).
 */
import { getToken, notifyAuthInvalid } from "../auth/token";
import type {
  LandingDetail,
  LandingFilters,
  LandingListResponse,
  LandingSummary,
} from "../types/api";

// Derived from the page's own URL (not a build-time constant) so the same
// build works both at the domain root and behind a reverse-proxy subpath
// (e.g. "/landing-teacher/"): "/foo/" -> "/foo", "/" -> "".
const BASE = new URL(".", window.location.href).pathname.replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Shared-token auth header (Issue #8); empty when no token is stored. */
function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { "X-Auth-Token": token } : {};
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { Accept: "application/json", ...authHeaders() },
  });
  if (!res.ok) {
    if (res.status === 401 || res.status === 403) notifyAuthInvalid();
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
