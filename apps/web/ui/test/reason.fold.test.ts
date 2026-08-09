/**
 * Reason-loop fold tests (docs/07 Phase 2): the kernel's reason scheduler
 * narrates itself via actor="reason" blackboard.delta events; folding them
 * must build the ReasonLoopView (recon → cycles → intents → finish) with the
 * documented status migrations, tolerate missing fields, ignore non-reason
 * events (reference-equal), and stay empty for legacy sessions.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  emptyDeck,
  EventType,
  reduce,
  type DeckState,
  type DSwarmEvent,
} from "../lib/events";
import {
  emptyReasonLoop,
  foldReasonEvent,
  type ReasonLoopView,
} from "../lib/reason";

let seq = 0;
function bb(kind: string, payload: Record<string, unknown>, ts = 1000): DSwarmEvent {
  return {
    event_type: EventType.BLACKBOARD_DELTA,
    seq: ++seq,
    ts,
    run_id: "run-test",
    payload: { kind, delta_type: kind, actor: "reason", ...payload },
  };
}

function foldAll(events: DSwarmEvent[]): ReasonLoopView {
  let loop = emptyReasonLoop();
  for (const ev of events) loop = foldReasonEvent(loop, ev);
  return loop;
}

describe("foldReasonEvent — full reason cycle", () => {
  const events: DSwarmEvent[] = [
    bb("recon_started", { intent_id: "recon", goal: "map the target", profile: "recon", task_kind: "recon" }, 1000),
    bb("recon_completed", { intent_id: "recon", duration_ms: 4200, new_findings: 3, flag: null, flags: [] }, 5200),
    bb("reason_cycle_started", { reason_cycle_id: "reason-1", generation: 1 }, 5300),
    bb("intent_proposed", {
      intent_id: "intent-1", goal: "enum /api routes", mode: "exec", priority: 8,
      profile: "web", surface_target: "http://t", task_kind: "web", host_scan: "nmap done",
      from_facts: [3, 7], dedupe_key: "web:/api", reason_cycle_id: "reason-1",
    }, 5400),
    bb("dispatch_decision", {
      intent_id: "intent-1", profile: "web", priority: 8,
      reason_cycle_id: "reason-1", dispatch_reason: "highest priority",
    }, 5500),
    bb("intent_completed", {
      intent_id: "intent-1", profile: "web", mode: "exec", reason_cycle_id: "reason-1",
      duration_ms: 9000, flag: "flag{reason_loop_works}",
    }, 14500),
    bb("reason_cycle_completed", {
      reason_cycle_id: "reason-1", generation: 1, duration_ms: 9200,
      audit_notes: ["coverage ok", "no dupes"], goal_met: true, planner: "deepseek",
    }, 14600),
    bb("reason_loop_finished", { stop_reason: "solved", solved: true, generations: 1 }, 14700),
  ];
  const loop = foldAll(events);

  it("tracks recon start/completion", () => {
    expect(loop.recon?.status).toBe("completed");
    expect(loop.recon?.startedAt).toBe(1000);
    expect(loop.recon?.durationMs).toBe(4200);
    expect(loop.recon?.newFindings).toBe(3);
    expect(loop.recon?.flag).toBeUndefined();
  });

  it("builds one cycle in arrival order with generation + planner", () => {
    expect(loop.cycles).toHaveLength(1);
    const cycle = loop.cycles[0];
    expect(cycle.id).toBe("reason-1");
    expect(cycle.generation).toBe(1);
    expect(cycle.status).toBe("completed");
    expect(cycle.startedAt).toBe(5300);
    expect(cycle.completedAt).toBe(14600);
    expect(cycle.durationMs).toBe(9200);
    expect(cycle.planner).toBe("deepseek");
    expect(cycle.goalMet).toBe(true);
  });

  it("migrates the intent proposed → running → completed", () => {
    const intent = loop.cycles[0].intents[0];
    expect(intent.id).toBe("intent-1");
    expect(intent.cycleId).toBe("reason-1");
    expect(intent.status).toBe("completed");
    expect(intent.goal).toBe("enum /api routes");
    expect(intent.mode).toBe("exec");
    expect(intent.priority).toBe(8);
    expect(intent.profile).toBe("web");
    expect(intent.surfaceTarget).toBe("http://t");
    expect(intent.taskKind).toBe("web");
    expect(intent.hostScan).toBe("nmap done");
    expect(intent.dedupeKey).toBe("web:/api");
    expect(intent.dispatchReason).toBe("highest priority");
    expect(intent.flag).toBe("flag{reason_loop_works}");
  });

  it("normalises from_facts seq numbers to fact:<seq> ids", () => {
    expect(loop.cycles[0].intents[0].fromFactIds).toEqual(["fact:3", "fact:7"]);
  });

  it("records audit notes and the loop finish", () => {
    expect(loop.cycles[0].audits).toEqual(["coverage ok", "no dupes"]);
    expect(loop.stopReason).toBe("solved");
    expect(loop.solved).toBe(true);
    expect(loop.paused).toBe(false);
  });
});

describe("foldReasonEvent — dedupe skip", () => {
  it("marks a skipped intent with its skip_reason", () => {
    const loop = foldAll([
      bb("reason_cycle_started", { reason_cycle_id: "reason-2", generation: 2 }),
      bb("intent_proposed", {
        intent_id: "intent-dup", goal: "retry sqli", mode: "exec",
        dedupe_key: "sqli:login", reason_cycle_id: "reason-2",
      }),
      bb("intent_skipped", {
        intent_id: "intent-dup", dedupe_key: "sqli:login",
        skip_reason: "duplicate of intent-1", reason_cycle_id: "reason-2",
      }),
    ]);
    const intent = loop.cycles[0].intents[0];
    expect(intent.status).toBe("skipped");
    expect(intent.skipReason).toBe("duplicate of intent-1");
    expect(intent.dedupeKey).toBe("sqli:login");
  });
});

describe("foldReasonEvent — fallback dispatch", () => {
  it("synthesises a running fallback-bootstrap intent", () => {
    const loop = foldAll([
      bb("reason_cycle_started", { reason_cycle_id: "reason-1", generation: 1 }),
      bb("fallback_dispatch", {
        intent_id: "fallback-bootstrap", reason: "planner proposed nothing",
        reason_cycle_id: "reason-1",
      }),
    ]);
    const intent = loop.cycles[0].intents.find((i) => i.id === "fallback-bootstrap");
    expect(intent).toBeDefined();
    expect(intent?.status).toBe("running");
    expect(intent?.dispatchReason).toBe("planner proposed nothing");
  });
});

describe("foldReasonEvent — pause + failure", () => {
  it("operator_paused flips paused; intent_failed marks the intent failed", () => {
    const loop = foldAll([
      bb("reason_cycle_started", { reason_cycle_id: "reason-1", generation: 1 }),
      bb("intent_proposed", { intent_id: "intent-x", goal: "fuzz", mode: "exec", reason_cycle_id: "reason-1" }),
      bb("dispatch_decision", { intent_id: "intent-x", reason_cycle_id: "reason-1" }),
      bb("operator_paused", {}),
      bb("intent_failed", { intent_id: "intent-x", reason_cycle_id: "reason-1", error: "worker oom" }),
    ]);
    expect(loop.paused).toBe(true);
    expect(loop.cycles[0].intents[0].status).toBe("failed");
  });
});

describe("foldReasonEvent — robustness", () => {
  it("returns the SAME reference for unrelated events", () => {
    const loop = emptyReasonLoop();
    const toolStart: DSwarmEvent = {
      event_type: EventType.TOOL_CALL_START,
      seq: 1, ts: 1, run_id: "run-test",
      payload: { tool: "bash" },
    };
    expect(foldReasonEvent(loop, toolStart)).toBe(loop);
  });

  it("returns the SAME reference for non-reason blackboard deltas", () => {
    const loop = emptyReasonLoop();
    const workerFact: DSwarmEvent = {
      event_type: EventType.BLACKBOARD_DELTA,
      seq: 1, ts: 1, run_id: "run-test",
      payload: { kind: "fact_added", actor: "worker-1", fact: "x" },
    };
    expect(foldReasonEvent(loop, workerFact)).toBe(loop);
    const reasonUnknown: DSwarmEvent = {
      event_type: EventType.BLACKBOARD_DELTA,
      seq: 2, ts: 2, run_id: "run-test",
      payload: { kind: "some_future_kind", actor: "reason" },
    };
    expect(foldReasonEvent(loop, reasonUnknown)).toBe(loop);
  });

  it("tolerates missing fields without throwing", () => {
    const loop = foldAll([
      bb("reason_cycle_started", {}),
      bb("intent_proposed", {}),
      bb("dispatch_decision", {}),
      bb("intent_completed", {}),
      bb("reason_cycle_completed", {}),
      bb("reason_loop_finished", {}),
    ]);
    expect(loop.cycles).toHaveLength(1);
    expect(loop.cycles[0].generation).toBe(0);
    expect(loop.cycles[0].status).toBe("completed");
    expect(loop.cycles[0].intents[0].status).toBe("completed");
    expect(loop.cycles[0].intents[0].fromFactIds).toEqual([]);
  });

  it("does not mutate the input loop", () => {
    const before = foldAll([
      bb("reason_cycle_started", { reason_cycle_id: "reason-1", generation: 1 }),
    ]);
    const after = foldReasonEvent(
      before,
      bb("intent_proposed", { intent_id: "i1", goal: "g", mode: "exec", reason_cycle_id: "reason-1" }),
    );
    expect(after).not.toBe(before);
    expect(before.cycles[0].intents).toHaveLength(0);
    expect(after.cycles[0].intents).toHaveLength(1);
  });
});

describe("legacy session replay — reasonLoop stays empty", () => {
  function loadFixture(name: string): DSwarmEvent[] {
    const path = fileURLToPath(new URL(`../test/fixtures/${name}`, import.meta.url));
    return readFileSync(path, "utf-8")
      .split("\n")
      .filter((line) => line.trim())
      .map((line) => JSON.parse(line) as DSwarmEvent);
  }

  it("legacy-race folds with no error and an empty reason loop", () => {
    const raw = loadFixture("legacy-race.session.jsonl");
    let state: DeckState = emptyDeck(raw[0]?.run_id ?? "unknown");
    for (const ev of raw) state = reduce(state, ev);
    expect(state.reasonLoop.cycles).toEqual([]);
    expect(state.reasonLoop.recon).toBeUndefined();
    expect(state.reasonLoop.paused).toBe(false);
    expect(state.reasonLoop.stopReason).toBeUndefined();
  });

  it("reduce folds reason deltas into DeckState.reasonLoop", () => {
    let state: DeckState = emptyDeck("run-test");
    for (const ev of [
      bb("reason_cycle_started", { reason_cycle_id: "reason-1", generation: 1 }),
      bb("intent_proposed", { intent_id: "i1", goal: "g", mode: "exec", reason_cycle_id: "reason-1" }),
      bb("operator_paused", {}),
    ]) {
      state = reduce(state, ev);
    }
    expect(state.reasonLoop.cycles).toHaveLength(1);
    expect(state.reasonLoop.cycles[0].intents[0].status).toBe("proposed");
    expect(state.reasonLoop.paused).toBe(true);
    // existing blackboard behaviour is undisturbed
    expect(state.blackboard.workers).toContain("reason");
  });
});
