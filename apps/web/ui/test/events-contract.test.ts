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
