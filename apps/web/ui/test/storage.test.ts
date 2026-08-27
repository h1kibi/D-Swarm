/**
 * Plain localStorage wrapper tests: reads return the stored value or null,
 * writes only touch the new `dswarm.*` key, removeKey clears it, and every
 * path is safe without a window (SSR) or with storage disabled.
 *
 * (The muteki.* → dswarm.* migration shim that used to live here was retired;
 * see lib/storage.ts for the historical note.)
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

describe("storage wrapper", () => {
  it("reads a written key back", () => {
    writeKey("dswarm.lang", "en");
    expect(readKey("dswarm.lang")).toBe("en");
  });

  it("returns null for unset keys", () => {
    expect(readKey("dswarm.theme")).toBeNull();
  });

  it("writes and clears dynamic blackboard layout keys", () => {
    writeKey("dswarm.bb.layout.v3.run-1", "{}");
    expect(readKey("dswarm.bb.layout.v3.run-1")).toBe("{}");
    removeKey("dswarm.bb.layout.v3.run-1");
    expect(readKey("dswarm.bb.layout.v3.run-1")).toBeNull();
  });

  it("removeKey deletes exactly one key", () => {
    store.setItem("dswarm_auth_token", "tok");
    removeKey("dswarm_auth_token");
    expect(readKey("dswarm_auth_token")).toBeNull();
    expect(store._map.size).toBe(0);
  });

  it("returns null and never throws without a window (SSR)", () => {
    delete (globalThis as Record<string, unknown>).window;
    expect(readKey("dswarm.lang")).toBeNull();
    expect(() => writeKey("dswarm.lang", "en")).not.toThrow();
    expect(() => removeKey("dswarm.lang")).not.toThrow();
  });

  it("never throws when storage rejects access", () => {
    const throwing = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("SecurityError");
      },
      removeItem: () => {
        throw new Error("SecurityError");
      },
    };
    (globalThis as Record<string, unknown>).window = { localStorage: throwing };
    expect(readKey("dswarm.lang")).toBeNull();
    expect(() => writeKey("dswarm.lang", "en")).not.toThrow();
    expect(() => removeKey("dswarm.lang")).not.toThrow();
  });
});
