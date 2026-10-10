import { afterEach, describe, expect, it, vi } from "vitest";
import { deleteRun } from "../lib/useRun";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("deleteRun", () => {
  it("only reports success when the API confirms deletion", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("/api/runs/run-delete-1");
      expect(init?.method).toBe("DELETE");
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(deleteRun("run-delete-1")).resolves.toBe(true);
  });

  it("rejects a successful HTTP response without an affirmative API payload", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ok: false }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));

    await expect(deleteRun("run-delete-1")).resolves.toBe(false);
  });
});
