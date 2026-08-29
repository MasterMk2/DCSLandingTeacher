/**
 * Typed fetch-based REST client for the landing API.
 * In dev, requests go through the Vite proxy (/api -> backend).
 */
import { getToken, notifyAuthInvalid } from "../auth/token";
import type {
  ImportJob,
  ImportStartResponse,
  LandingDetail,
  LandingFilters,
  LandingListResponse,
  LandingSummary,
} from "../types/api";

// Derived from the page's own URL (not a build-time constant) so the same
// build works both at the domain root and behind a reverse-proxy subpath
// (e.g. "/landing-teacher/"): "/foo/" -> "/foo", "/" -> "".
const BASE = new URL(".", window.location.href).pathname.replace(/\/$/, "");

// API version prefix (Issue #38). Pinned to v1 so the contract is stable; a
// future breaking change ships under /api/v2 without affecting this client.
const API_V1 = "/api/v1";

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
    `${API_V1}/landings${buildQuery(filters, limit, offset)}`,
  );
}

export function getLanding(id: number): Promise<LandingDetail> {
  return getJson<LandingDetail>(`${API_V1}/landings/${id}`);
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

// ---------------------------------------------------------------------------
// ACMI file import (background jobs)
// ---------------------------------------------------------------------------

/** Upload an ACMI recording; the backend processes it in the background. */
export async function importAcmiFile(file: File): Promise<ImportStartResponse> {
  const body = new FormData();
  body.append("file", file);
  const res = await fetch(`${BASE}${API_V1}/import`, {
    method: "POST",
    headers: authHeaders(),
    body,
  });
  if (!res.ok) {
    if (res.status === 401 || res.status === 403) notifyAuthInvalid();
    throw new ApiError(res.status, `POST ${API_V1}/import failed: ${res.status}`);
  }
  return (await res.json()) as ImportStartResponse;
}

export function getImport(jobId: string): Promise<ImportJob> {
  return getJson<ImportJob>(`${API_V1}/imports/${jobId}`);
}

export function listImports(): Promise<{ items: ImportJob[] }> {
  return getJson<{ items: ImportJob[] }>(`${API_V1}/imports`);
}

/** Discard an import and everything it created.
 *
 *  Uploads are scratch data: they are usually a recording from some other
 *  server, so they are kept out of the shared history and thrown away once
 *  the user is done looking at them.
 */
export async function discardImport(jobId: string): Promise<void> {
  const res = await fetch(`${BASE}/api/imports/${jobId}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok && res.status !== 404) {
    if (res.status === 401 || res.status === 403) notifyAuthInvalid();
    throw new ApiError(res.status, `DELETE /api/imports failed: ${res.status}`);
  }
}

/** Best-effort discard for a page that is going away.
 *
 *  `fetch` is cancelled while unloading, so this uses sendBeacon, which the
 *  browser is allowed to finish afterwards. It cannot issue a DELETE, hence
 *  the dedicated POST route. The server-side retention sweep covers whatever
 *  still slips through (a crash, a killed tab).
 */
export function discardImportOnUnload(jobId: string): void {
  const url = `${BASE}/api/imports/${jobId}/discard`;
  if (typeof navigator !== "undefined" && navigator.sendBeacon) {
    navigator.sendBeacon(url, new Blob([], { type: "text/plain" }));
    return;
  }
  void discardImport(jobId).catch(() => undefined);
}
