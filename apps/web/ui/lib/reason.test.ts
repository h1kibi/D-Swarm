import { describe, expect, it } from "vitest";
import {
  emptyReasonLoop,
  formatDurationMs,
  reasonCycleRows,
  reasonLoopTone,
} from "./reason";

describe("reason loop presentation projections", () => {
  it("tones: solved ok, operator stop muted, dry/other stops warn, paused warn", () => {
    expect(reasonLoopTone({ cycles: [], paused: false, solved: true })).toBe("ok");
    expect(reasonLoopTone({ cycles: [], paused: false, stopReason: "operator_stop" })).toBe("muted");
    expect(reasonLoopTone({ cycles: [], paused: false, stopReason: "reason_dry" })).toBe("warn");
    expect(reasonLoopTone({ cycles: [], paused: true })).toBe("warn");
    expect(reasonLoopTone(emptyReasonLoop())).toBe("ok");
  });

  it("cycle rows expose status, duration, trigger and counts", () => {
    const loop = {
      cycles: [
        {
          id: "reason-1",
          generation: 1,
          status: "completed" as const,
          startedAt: 100,
          completedAt: 194.5,
          durationMs: 94900,
          trigger: "run_start",
          audits: ["a1", "a2"],
          intents: [],
        },
        {
          id: "reason-2",
          generation: 2,
          status: "running" as const,
          startedAt: 200,
          audits: [],
          intents: [],
        },
      ],
      paused: false,
    };
    const rows = reasonCycleRows(loop);
    expect(rows[0]).toMatchObject({
      id: "reason-1", generation: 1, status: "completed",
      durationMs: 94900, trigger: "run_start", auditCount: 2, intentCount: 0,
    });
    // a running cycle derives duration from now — just assert it is non-negative
    expect(rows[1].durationMs).toBeGreaterThanOrEqual(0);
  });

  it("formats durations for the strip", () => {
    expect(formatDurationMs(undefined)).toBe("—");
    expect(formatDurationMs(4)).toBe("4ms");
    expect(formatDurationMs(94921)).toBe("1m35s");
    expect(formatDurationMs(125_000)).toBe("2m5s");
  });
});
