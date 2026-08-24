import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  clearToken,
  getToken,
  saveToken,
  setTokenStorage,
} from "./token";

function makeFakeStorage(): Storage & { store: Map<string, string> } {
  const store = new Map<string, string>();
  return {
    store,
    getItem: (key) => (store.has(key) ? (store.get(key) as string) : null),
    setItem: (key, value) => void store.set(key, value),
    removeItem: (key) => void store.delete(key),
    clear: () => void store.clear(),
    key: () => null,
    get length() {
      return store.size;
    },
  };
}

describe("token storage", () => {
  let fake: ReturnType<typeof makeFakeStorage>;

  beforeEach(() => {
    fake = makeFakeStorage();
    setTokenStorage(fake as unknown as Storage);
  });

  afterEach(() => {
    setTokenStorage(undefined);
  });

  it("returns an empty string when no token is stored", () => {
    expect(getToken()).toBe("");
  });

  it("saves and reads back a token", () => {
    saveToken("secret-token");
    expect(getToken()).toBe("secret-token");
    expect(fake.store.get("dlt.authToken")).toBe("secret-token");
  });

  it("overwrites a previously saved token", () => {
    saveToken("first");
    saveToken("second");
    expect(getToken()).toBe("second");
  });

  it("clears the stored token", () => {
    saveToken("secret-token");
    clearToken();
    expect(getToken()).toBe("");
    expect(fake.store.has("dlt.authToken")).toBe(false);
  });
});
