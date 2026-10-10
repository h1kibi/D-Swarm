import { describe, expect, it } from "vitest";
import { emptyDeck, EventType, reduce, type DSwarmEvent } from "../lib/events";
import { projectRouteLineage } from "../lib/lineage";
import { projectObserverSummary } from "../lib/observerSummary";

function bb(payload: Record<string, unknown>, seq = 1, ts = 1, solverId = "reason"): DSwarmEvent {
  return {
    event_type: EventType.BLACKBOARD_DELTA,
    seq,
    ts,
    run_id: "run-lineage",
    solver_id: solverId,
    payload: { actor: solverId, ...payload },
  } as DSwarmEvent;
}

function explicitDeck() {
  let s = emptyDeck("run-lineage");
  s = reduce(s, bb({ kind: "reason_cycle_started", reason_cycle_id: "reason-1", generation: 1, lineage_from: "run:run-lineage", lineage_to: "reason:reason-1" }, 1));
  s = reduce(s, bb({
    kind: "intent_proposed", intent_id: "I1", goal: "enumerate /api",
    reason_cycle_id: "reason-1", lineage_from: "reason:reason-1", lineage_to: "intent:I1",
  }, 2));
  s = reduce(s, bb({ kind: "intent_claimed", intent_id: "I1", worker: "cli-pi#1", source_event_seq: 3 }, 3, 3, "cli-pi#1"));
  s = reduce(s, bb({ kind: "intent_concluded", intent_id: "I1", worker: "cli-pi#1", result: "explored", to_fact_seq: 7, source_event_seq: 4 }, 4, 4, "cli-pi#1"));
  s = reduce(s, bb({ kind: "fact_added", fact_seq: 7, fact: "GET /api/users returns JSON", verified: true, intent_id: "I1" }, 5, 5, "cli-pi#1"));
  return s;
}

describe("route lineage projection", () => {
  it("projects the explicit run → reason → intent → worker → fact chain", () => {
    const lineage = projectRouteLineage(explicitDeck());

    expect(lineage.explicit).toBe(true);
    expect(lineage.edges.map((e) => `${e.from}->${e.to}:${e.kind}`).sort()).toEqual([
      "intent:I1->worker:cli-pi#1:claimed",
      "reason:reason-1->intent:I1:planned",
      "run:run-lineage->reason:reason-1:planned",
      "worker:cli-pi#1->fact:7:produced",
    ]);
    expect(lineage.roots).toEqual(["run:run-lineage"]);
    expect(lineage.legacyIntentIds).toEqual([]);
  });

  it("is order-independent: shuffling cycles never changes parent/child", () => {
    const deck = explicitDeck();
    const shuffled = {
      ...deck,
      reasonLoop: { ...deck.reasonLoop, cycles: [...deck.reasonLoop.cycles].reverse() },
      blackboard: { ...deck.blackboard, lineageEdges: [...deck.blackboard.lineageEdges].reverse() },
    };

    const a = projectRouteLineage(deck);
    const b = projectRouteLineage(shuffled);
    expect(b.edges.map((e) => `${e.from}->${e.to}:${e.kind}`).sort())
      .toEqual(a.edges.map((e) => `${e.from}->${e.to}:${e.kind}`).sort());
    expect(b.roots).toEqual(a.roots);
  });

  it("stays legacy when no edge was published, without inventing relations", () => {
    let s = emptyDeck("run-lineage");
    s = reduce(s, bb({ kind: "reason_cycle_started", reason_cycle_id: "reason-1", generation: 1 }, 1));
    s = reduce(s, bb({ kind: "intent_proposed", intent_id: "I9", goal: "guess", reason_cycle_id: "reason-1" }, 2));

    const lineage = projectRouteLineage(s);
    expect(lineage.explicit).toBe(false);
    expect(lineage.edges).toEqual([]);
    expect(lineage.roots).toEqual([]);
    expect(lineage.legacyIntentIds).toEqual(["I9"]);
  });

  it("never fabricates branch identity from array position", () => {
    let s = emptyDeck("run-lineage");
    s = reduce(s, bb({ kind: "branch_split", branch_id: "branch-parent", title: "two hypotheses", branches: [{ assumption: "a" }, { assumption: "b" }] }, 1));

    expect(s.blackboard.branches.map((b) => b.branchId)).toEqual(["branch-parent"]);
    expect(projectRouteLineage(s).edges).toEqual([]);

    const withIds = reduce(s, bb({
      kind: "branch_split", branch_id: "branch-parent", title: "with ids",
      branches: [{ id: "branch-a" }, { id: "branch-b" }],
    }, 2));
    expect(projectRouteLineage(withIds).edges.map((e) => `${e.from}->${e.to}`).sort())
      .toEqual(["branch:branch-parent->branch:branch-a", "branch:branch-parent->branch:branch-b"]);
  });

  it("ignores a route event that publishes no explicit edge endpoints", () => {
    const s = reduce(emptyDeck("run-lineage"), bb({ kind: "route_suppressed", route_hash: "web:login:sqli", reason: "no new evidence" }, 1));

    expect(s.blackboard.suppressedRoutes.map((r) => r.routeHash)).toEqual(["web:login:sqli"]);
    expect(projectRouteLineage(s).edges).toEqual([]);
  });
});

describe("worker runtime diagnostics fold", () => {
  it("keeps bounded stdout/stderr/artifacts across status updates", () => {
    let s = emptyDeck("run-lineage");
    s = reduce(s, {
      event_type: EventType.WORKER_STATUS, seq: 1, ts: 1, run_id: "run-lineage", solver_id: "cli-pi#1",
      payload: {
        online: false, status: "offline", reason: "finished", worker_role: "explore",
        runtime: {
          backend: "container", status: "finished", stderr: "boom: provider unreachable",
          stdout: "GET /flag -> flag{real}", artifact_ids: ["art-1", "art-2"],
          no_feedback: false, real_output_without_flag: true,
        },
      },
    } as DSwarmEvent);

    const lane = s.lanes["cli-pi#1"];
    expect(lane.runtime?.stdout).toContain("flag{real}");
    expect(lane.runtime?.stderr).toContain("provider unreachable");
    expect(lane.runtime?.artifact_ids).toEqual(["art-1", "art-2"]);
    expect(lane.runtime?.real_output_without_flag).toBe(true);
    expect(lane.online).toBe(false);
  });
});

describe("observer summary projection", () => {
  function deckWithWork() {
    let s = emptyDeck("run-obs");
    s = reduce(s, bb({ kind: "reason_cycle_started", reason_cycle_id: "reason-1", generation: 1 }, 1));
    s = reduce(s, bb({ kind: "intent_proposed", intent_id: "I1", goal: "enumerate /api", lineage_from: "reason:reason-1", lineage_to: "intent:I1" }, 2));
    s = reduce(s, bb({ kind: "intent_claimed", intent_id: "I1", worker: "cli-pi#1" }, 3, 3, "cli-pi#1"));
    s = reduce(s, bb({ kind: "fact_added", fact_seq: 7, fact: "GET /api/users -> 200", verified: true, intent_id: "I1" }, 4, 4, "cli-pi#1"));
    s = reduce(s, bb({ kind: "fact_added", fact_seq: 8, fact: "maybe /admin reachable", verified: false, intent_id: "I1" }, 5, 5, "cli-pi#1"));
    return s;
  }

  it("reports real counts and the next decider without inventing state", () => {
    const summary = projectObserverSummary(deckWithWork(), null);

    expect(summary.evidence.find((row) => row.label === "Verified facts")?.value).toBe("1");
    expect(summary.evidence.find((row) => row.label === "Candidates")?.value).toBe("1");
    expect(summary.activity.find((row) => row.label === "Active intents")?.value).toBe("1");
    expect(summary.nextDecider.value).toContain("Reason");
    expect(summary.health.warn).toBe(false);
  });

  it("surfaces a Reason block as operator attention instead of a healthy run", () => {
    let s = deckWithWork();
    s = reduce(s, bb({ kind: "provider_recovery_blocked", role: "reason", blocked_reason: "no_healthy_candidate" }, 6));

    const summary = projectObserverSummary(s, null);
    expect(summary.health.status).toBe("blocked");
    expect(summary.health.warn).toBe(true);
    expect(summary.reason.warn).toBe(true);
    expect(summary.nextDecider.value).toContain("Operator");
    expect(summary.operatorAttention.length).toBeGreaterThan(0);
  });

  it("flags parked interruptions and puts recovery first", () => {
    let s = deckWithWork();
    s = reduce(s, bb({ kind: "intent_interrupted", intent_id: "I1", worker: "cli-pi#1", dispatch_state: "awaiting_validation" }, 6, 6, "reason"));

    const summary = projectObserverSummary(s, null);
    expect(summary.activity.find((row) => row.label === "Awaiting validation")?.value).toBe("1");
    expect(summary.nextDecider.state).toBe("awaiting_validation");
    expect(summary.operatorAttention.some((row) => row.includes("awaiting_validation"))).toBe(true);
  });
});

describe("observer summary attention dedup", () => {
  it("keeps the blocked-Reason line even when another row also mentions Reason", () => {
    let s = emptyDeck("run-dedup");
    s = reduce(s, bb({ kind: "reason_cycle_started", reason_cycle_id: "reason-1", generation: 1 }, 1));
    s = reduce(s, bb({ kind: "intent_proposed", intent_id: "I1", goal: "probe", lineage_from: "reason:reason-1", lineage_to: "intent:I1" }, 2));
    s = reduce(s, bb({ kind: "intent_claimed", intent_id: "I1", worker: "cli-pi#1" }, 3, 3, "cli-pi#1"));
    s = reduce(s, bb({ kind: "intent_interrupted", intent_id: "I1", worker: "cli-pi#1", dispatch_state: "awaiting_validation" }, 4, 4, "reason"));
    s = reduce(s, bb({ kind: "provider_recovery_blocked", role: "reason", blocked_reason: "no_healthy_candidate" }, 5));

    const rows = projectObserverSummary(s, null).operatorAttention;
    expect(rows.some((row) => row.includes("awaiting_validation"))).toBe(true);
    expect(rows.some((row) => row.startsWith("Reason blocked"))).toBe(true);
  });
});
