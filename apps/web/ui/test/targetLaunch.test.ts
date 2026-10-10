import { afterEach, describe, expect, it, vi } from "vitest";
import { workerBackendOverride } from "../lib/launch";
import { createTargetRun } from "../lib/useTargets";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Target Run launch contract", () => {
  it("keeps the backend omitted until the operator explicitly chooses one", () => {
    expect(workerBackendOverride(undefined)).toEqual({});
    expect(workerBackendOverride(true)).toEqual({ worker_backend: "container" });
    expect(workerBackendOverride(false)).toEqual({ worker_backend: "local" });
  });

  it("surfaces a safe target-run validation detail", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 400,
      json: async () => ({ detail: "direction_id does not belong to target" }),
    } as Response));
    vi.stubGlobal("fetch", fetchMock);

    await expect(createTargetRun("target-1")).rejects.toThrow("direction_id does not belong to target");
  });

  it("returns the created Run id without converting a valid response to null", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ run: { run_id: "run-created" } }),
    } as Response));
    vi.stubGlobal("fetch", fetchMock);

    await expect(createTargetRun("target-1", { include_target_hints: false })).resolves.toEqual({ run_id: "run-created" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/targets/target-1/runs",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ include_target_hints: false }),
      }),
    );
  });
});
