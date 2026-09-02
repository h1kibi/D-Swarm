import { describe, expect, it } from "vitest";
import { emptyDeck, EventType, reduce, type DSwarmEvent } from "../lib/events";

function blackboardEvent(payload: Record<string, unknown>) {
  return {
    event_type: EventType.BLACKBOARD_DELTA,
    seq: 1,
    ts: 1,
    run_id: "run-contract",
    solver_id: "solver-contract",
    payload,
  } as DSwarmEvent;
}

describe("blackboard event contract", () => {
  it("renders unknown kinds as generic timeline activity without typed state", () => {
    const previous = emptyDeck("run-contract");
    const state = reduce(previous, blackboardEvent({
      kind: "future_kind",
      actor: "worker-x",
      arbitrary: "ignored",
    }));

    expect(state.blackboard.intents).toEqual([]);
    expect(state.blackboard.facts).toEqual([]);
    expect(state.blackboard.pocs).toEqual([]);
    expect(state.blackboard.flags).toEqual([]);
    expect(state.blackboard.workers).toEqual([]);
    expect(state.model).toEqual(previous.model);
    expect(state.blackboard.events.at(-1)).toMatchObject({
      kind: "future_kind",
      actor: "worker-x",
      label: "unrecognized blackboard event: future_kind",
    });
  });

  it.each([
    [undefined, "(missing kind)"],
    [null, "(missing kind)"],
    [42, "(missing kind)"],
    ["   ", "(missing kind)"],
  ])("does not throw for malformed kind %p", (kind, label) => {
    const state = reduce(emptyDeck("run-contract"), blackboardEvent({
      kind,
      actor: "worker-x",
    }));

    expect(state.blackboard.events.at(-1)).toMatchObject({
      kind: "(missing kind)",
      label: `unrecognized blackboard event: ${label}`,
    });
  });
});

describe("run finish messaging", () => {
  const ev = (seq: number, event_type: EventType, payload: Record<string, unknown>, solver_id?: string) =>
    ({ event_type, seq, ts: seq, run_id: "run-f", solver_id, payload } as DSwarmEvent);

  it("does not claim 'no flag' after a solved banner already ran", () => {
    // multi-flag shape (run-6203): worker.finish banners the solve first, then
    // the trailing run.finished folded into the no-flag else branch.
    let s = emptyDeck("run-f");
    s = reduce(s, ev(1, EventType.WORKER_FINISHED, { flag: "afctf{c14ssic_cae5ar}", solved: true }, "cli-pi"));
    s = reduce(s, ev(2, EventType.RUN_FINISHED, { solved: true, flag: "NSSCTF{c14ssic_cae5ar}" }));
    const keys = s.chat.map((m) => m.i18nKey);
    expect(keys).toContain("sys.solved");
    expect(keys).not.toContain("sys.finishedNoFlag");
  });

  it("still reports a no-flag finish for genuinely flagless runs", () => {
    let s = emptyDeck("run-g");
    s = reduce(s, ev(1, EventType.RUN_FINISHED, { solved: false }));
    expect(s.chat.map((m) => m.i18nKey)).toContain("sys.finishedNoFlag");
  });
});
