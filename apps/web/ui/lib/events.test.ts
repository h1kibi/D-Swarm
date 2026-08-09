/**
 * Reducer smoke tests — the regression floor for the event-normalizer work
 * (docs/07 Phase 1). The reducer must stay total: old sessions (missing
 * fields) and future kernels (unknown event types / extra payload fields)
 * must fold without throwing.
 */
import { describe, expect, it } from "vitest";
import { emptyDeck, EventType, reduce, type DSwarmEvent } from "./events";

function ev(
  event_type: EventType | string,
  payload: Record<string, unknown> = {},
  over: Partial<DSwarmEvent> = {},
): DSwarmEvent {
  return {
    event_type: event_type as EventType,
    seq: 1,
    ts: 1723000000,
    run_id: "run-test",
    payload,
    ...over,
  };
}

describe("reduce", () => {
  it("folds run.started into an opened thread", () => {
    const s0 = emptyDeck("run-test");
    const s1 = reduce(
      s0,
      ev(EventType.RUN_STARTED, {
        challenge: {
          name: "web-042",
          category: "web",
          target: "https://example.test/",
          expected_flags: 2,
          multi_flag: true,
        },
      }),
    );
    expect(s1.started).toBe(true);
    expect(s1.challengeName).toBe("web-042");
    expect(s1.expectedFlags).toBe(2);
    expect(s1.multiFlag).toBe(true);
  });

  it("folds worker.status into a lane", () => {
    const s0 = emptyDeck("run-test");
    const s1 = reduce(
      s0,
      ev(
        EventType.WORKER_STATUS,
        { online: true, status: "online" },
        { solver_id: "pi-worker-1" },
      ),
    );
    expect(s1.lanes["pi-worker-1"]?.online).toBe(true);
  });

  it("tolerates missing payload fields (legacy sessions)", () => {
    const s0 = emptyDeck("run-test");
    expect(() =>
      reduce(s0, ev(EventType.RUN_STARTED, {})),
    ).not.toThrow();
    expect(() =>
      reduce(s0, ev(EventType.WORKER_STATUS, {})),
    ).not.toThrow();
    expect(() =>
      reduce(s0, ev(EventType.BLACKBOARD_DELTA, {})),
    ).not.toThrow();
  });

  it("ignores unknown event types (forward compatibility)", () => {
    const s0 = emptyDeck("run-test");
    let s1 = s0;
    expect(() => {
      s1 = reduce(s0, ev("some.future.event", { whatever: 1 }));
    }).not.toThrow();
    // state may be re-copied by the reducer, but nothing may change
    expect(s1.started).toBe(s0.started);
    expect(s1.challengeName).toBe(s0.challengeName);
    expect(Object.keys(s1.lanes)).toHaveLength(0);
  });
});
