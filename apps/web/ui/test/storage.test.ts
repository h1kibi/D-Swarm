/**
 * Storage key migration (docs/07 Phase 3): `muteki.*` → `dswarm.*`. Reads must
 * prefer the new key and fall back to the legacy one (no user data loss);
 * writes must only touch the new key; removeKey clears both.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { readKey, writeKey, removeKey } from "../lib/storage";

// Minimal window/localStorage stub for the node test environment.
function fakeStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
    _map: map,
  };
}

let store: ReturnType<typeof fakeStorage>;

beforeEach(() => {
  store = fakeStorage();
  (globalThis as Record<string, unknown>).window = { localStorage: store };
});

describe("storage key migration", () => {
  it("reads the new key when present", () => {
    store.setItem("dswarm.lang", "en");
    store.setItem("muteki.lang", "zh");
    expect(readKey("dswarm.lang")).toBe("en");
  });

  it("falls back to the legacy key", () => {
    store.setItem("muteki.lang", "zh");
    expect(readKey("dswarm.lang")).toBe("zh");
  });

  it("falls back for dynamic blackboard layout keys", () => {
    store.setItem("muteki.bb.layout.v3.run-1", "{}");
    expect(readKey("dswarm.bb.layout.v3.run-1")).toBe("{}");
  });

  it("writes only the new key", () => {
    writeKey("dswarm.theme", "dark");
    expect(store.getItem("dswarm.theme")).toBe("dark");
    expect(store.getItem("muteki.theme")).toBeNull();
  });

  it("removeKey clears both new and legacy", () => {
    store.setItem("dswarm_auth_token", "new");
    store.setItem("muteki_auth_token", "old");
    removeKey("dswarm_auth_token");
    expect(readKey("dswarm_auth_token")).toBeNull();
  });

  it("returns null without a window (SSR)", () => {
    delete (globalThis as Record<string, unknown>).window;
    expect(readKey("dswarm.lang")).toBeNull();
    expect(() => writeKey("dswarm.lang", "en")).not.toThrow();
  });
});
