import { describe, expect, it } from "vitest";
import {
  poolProblem,
  poolTone,
  runtimeSummary,
  type RuntimePoolStatus,
} from "./runtimeStatus";

const pool = (over: Partial<RuntimePoolStatus> = {}): RuntimePoolStatus => ({
  pool_id: "pool-v1__abc",
  state: "ready",
  generation: 1,
  pool_instance_id: "instance-9",
  active_workers: 1,
  waiting_workers: 0,
  capacity: 3,
  failure: null,
  recovery_episode: 0,
  ...over,
});

describe("RuntimeStatus projections", () => {
  it("tones ready pools ok and degraded pools warn", () => {
    expect(poolTone(pool())).toBe("ok");
    expect(poolTone(pool({ state: "degraded" }))).toBe("warn");
    expect(poolTone(pool({ state: "probing" }))).toBe("warn");
  });

  it("paints any pool carrying a failure bad", () => {
    expect(poolTone(pool({ failure: { category: "auth", code: "probe_denied" } }))).toBe("bad");
  });

  it("summarises ready/total pools and workers/capacity", () => {
    const snapshot = {
      run_id: "run-1",
      policy_mode: "docker",
      pools: [pool(), pool({ state: "probing", active_workers: 0 })],
    };
    expect(runtimeSummary(snapshot)).toBe("1/2 1/6");
    expect(runtimeSummary({ run_id: "run-1", policy_mode: "", pools: [] })).toBe("—");
  });

  it("surfaces the live failure, else the last history failure", () => {
    expect(poolProblem(pool({
      failure: { category: "auth", code: "probe_denied" },
      history: [{ state: "degraded", failure: { category: "auth", code: "older" } }],
    }))).toBe("auth:probe_denied");
    expect(poolProblem(pool({
      history: [{ state: "degraded", failure: { category: "infrastructure", code: "timeout" } }],
    }))).toBe("infrastructure:timeout");
    expect(poolProblem(pool())).toBeNull();
  });
});
