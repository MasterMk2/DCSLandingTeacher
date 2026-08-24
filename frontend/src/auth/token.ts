/**
 * Shared-token storage for the simple API authentication (Issue #8).
 *
 * The token is kept in localStorage and attached by the REST client
 * (`X-Auth-Token`) and the WebSocket client (`?token=`). When the backend
 * answers 401/403 the REST client dispatches {@link AUTH_INVALID_EVENT} so
 * the app can prompt for a fresh token.
 */

const STORAGE_KEY = "dlt.authToken";

/** Minimal subset of the DOM Storage interface (for test injection). */
export interface TokenStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

let storageOverride: TokenStorage | null | undefined;

function defaultStorage(): TokenStorage | null {
  try {
    if (typeof globalThis.localStorage !== "undefined") {
      return globalThis.localStorage;
    }
  } catch {
    // Accessing localStorage can throw in some privacy modes.
  }
  return null;
}

function storage(): TokenStorage | null {
  return storageOverride === undefined ? defaultStorage() : storageOverride;
}

/** Testing hook: inject a storage backend (pass undefined to reset). */
export function setTokenStorage(s: TokenStorage | null | undefined): void {
  storageOverride = s;
}

export function getToken(): string {
  return storage()?.getItem(STORAGE_KEY) ?? "";
}

export function saveToken(token: string): void {
  storage()?.setItem(STORAGE_KEY, token);
}

export function clearToken(): void {
  storage()?.removeItem(STORAGE_KEY);
}

/** Window event name dispatched when the API rejects the stored token. */
export const AUTH_INVALID_EVENT = "dlt:auth-invalid";

export function notifyAuthInvalid(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(AUTH_INVALID_EVENT));
  }
}
