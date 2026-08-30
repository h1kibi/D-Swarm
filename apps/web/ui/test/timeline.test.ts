/**
 * Decision Timeline assembly tests (docs/07 §5.3/§5.4, Phase 4): mixed
 * reasonLoop + chat + legacy ordering, cycle-card data, directive lifecycle
 * migration, stage derivation (explicit reason loop vs legacy fallback), and
 * stage anchors for the Stage Rail jump.
 */
import { describe, expect, it } from "vitest";
import { emptyDeck, type ChatMessage, type DeckState } from "../lib/events";
import type { ReasonLoopView } from "../lib/reason";
import {
  buildTimeline,
  deriveStage,
  directiveLifecycle,
  isTimelineChat,
  stageAnchors,
  stageRailStates,
} from "../lib/timeline";

function chat(id: string, role: ChatMessage["role"], kind: ChatMessage["kind"], ts: number, mainThread?: boolean): ChatMessage {
  return { id, role, kind, content: `${kind} ${id}`, ts, mainThread };
}

/** A deck carrying a two-cycle reason loop with dispatched + proposed intents. */
function deckWithLoop(): DeckState {
  const deck = emptyDeck("run-t");
  deck.started = true;
  deck.startedAt = 1000;
  const loop: ReasonLoopView = {
    paused: false,
    recon: { status: "completed", startedAt: 1000, durationMs: 2000, newFindings: 3 },
    cycles: [
      {
        id: "reason-1",
        generation: 1,
        status: "completed",
        startedAt: 2000,
        completedAt: 2600,
        durationMs: 600,
        audits: ["fact #41 verified", "jwt still candidate"],
        intents: [
          {
            id: "I-14",
            cycleId: "reason-1",
            goal: "Probe /api/admin auth boundary",
            mode: "explore",
            priority: 0.91,
            status: "running",
            fromFactIds: ["fact:41", "fact:44"],
            workerId: "pi-worker-2",
            dispatchedAt: 2700,
            profile: "web-explore",
          },
          {
            id: "I-15",
            cycleId: "reason-1",
            goal: "Enumerate admin routes",
            mode: "exec",
            priority: 0.73,
            status: "proposed",
            fromFactIds: ["fact:41"],
          },
        ],
      },
    ],
  };
  deck.reasonLoop = loop;
  return deck;
}

describe("buildTimeline — reason loop + chat + legacy mixed ordering", () => {
  it("orders recon → cycle → dispatch → flag and chat by ts with kind priority", () => {
    const deck = deckWithLoop();
    deck.blackboard.facts.push({
      factSeq: 44, fact: "/admin accepts unsigned token", verified: true,
      confidence: 0.9, actor: "pi-worker-2", verifier: "reason", ts: 2800,
    });
    deck.blackboard.events.push(
      { id: "e1", kind: "flag_found", actor: "pi-worker-2", ts: 3000, label: "flag{jwt_none}" },
      { id: "e0", kind: "race_started", actor: "coordinator", ts: 900, label: "race started" },
    );
    deck.chat.push(
      chat("c1", "human", "text", 2500),
      chat("c2", "agent", "reasoning", 2550), // firehose — excluded
      chat("c3", "system", "status", 2900),
    );

    const items = buildTimeline(deck);
    const kinds = items.map((i) => i.kind);
    expect(kinds).toEqual([
      "legacy",   // ts 900
      "recon",    // ts 1000
      "cycle",    // ts 2000
      "chat",     // ts 2500 (human prompt is a timeline event)
      "dispatch", // ts 2700 (dispatchedAt)
      "fact",     // ts 2800
      "chat",     // ts 2900
      "flag",     // ts 3000
    ]);
    // the proposed (never dispatched) intent I-15 produces no dispatch item
    expect(items.filter((i) => i.kind === "dispatch")).toHaveLength(1);
  });

  it("keeps cycle-card data: audits, intent priority / from_facts / status", () => {
    const deck = deckWithLoop();
    const cycleItem = buildTimeline(deck).find((i) => i.kind === "cycle");
    expect(cycleItem && cycleItem.kind === "cycle" && cycleItem.cycle.audits).toHaveLength(2);
    const dispatch = buildTimeline(deck).find((i) => i.kind === "dispatch");
    if (!dispatch || dispatch.kind !== "dispatch") throw new Error("no dispatch item");
    expect(dispatch.intent.priority).toBe(0.91);
    expect(dispatch.intent.fromFactIds).toEqual(["fact:41", "fact:44"]);
    expect(dispatch.intent.workerId).toBe("pi-worker-2");
    expect(dispatch.stage).toBe("dispatch");
  });

  it("legacy sessions degrade to chat + legacy activity markers", () => {
    const deck = emptyDeck("run-old");
    deck.started = true;
    deck.startedAt = 100;
    deck.blackboard.events.push(
      { id: "r1", kind: "race_started", actor: "coordinator", ts: 110, label: "race started" },
      { id: "r2", kind: "race_concluded", actor: "coordinator", ts: 120, label: "race done" },
      { id: "f1", kind: "fact_added", actor: "w1", ts: 130, label: "some fact" },
    );
    deck.chat.push(chat("m1", "human", "text", 115));
    const items = buildTimeline(deck);
    expect(items.map((i) => i.kind)).toEqual(["legacy", "chat", "legacy"]);
    const legacy = items[0];
    if (legacy.kind !== "legacy") throw new Error("expected legacy item");
    expect(legacy.i18nKey).toBe("legacy.raceStarted");
    // fact_added is NOT a legacy kind — no item from the blackboard log itself
    expect(items.some((i) => i.id === "legacy:f1")).toBe(false);
  });

  it("surfaces operator directives with their lifecycle", () => {
    const deck = deckWithLoop();
    deck.operatorDirectives.push(
      { id: "d1", text: "focus jwt", action: "focus", status: "received", ts: 2500 },
      { id: "d2", text: "drop ftp", action: "directive", status: "acted", ts: 2600 },
    );
    const directives = buildTimeline(deck).filter((i) => i.kind === "directive");
    expect(directives).toHaveLength(2);
    expect(directives.map((d) => d.kind === "directive" && d.lifecycle)).toEqual(["queued", "applied"]);
  });
});

describe("directiveLifecycle", () => {
  it("maps kernel directive statuses onto the UI lifecycle", () => {
    expect(directiveLifecycle("received")).toBe("queued");
    expect(directiveLifecycle("queued")).toBe("queued");
    expect(directiveLifecycle("bound")).toBe("consumed");
    expect(directiveLifecycle("acted")).toBe("applied");
    expect(directiveLifecycle("superseded")).toBe("closed");
    expect(directiveLifecycle("expired")).toBe("closed");
    expect(directiveLifecycle("rejected")).toBe("closed");
  });
});

describe("isTimelineChat", () => {
  it("keeps human/system and main-thread agent messages, drops worker firehose", () => {
    expect(isTimelineChat(chat("a", "human", "text", 1))).toBe(true);
    expect(isTimelineChat(chat("b", "system", "status", 1))).toBe(true);
    expect(isTimelineChat(chat("c", "agent", "text", 1, true))).toBe(true);
    expect(isTimelineChat(chat("d", "agent", "reasoning", 1))).toBe(false);
    expect(isTimelineChat(chat("e", "agent", "tool", 1))).toBe(false);
  });
});

describe("deriveStage", () => {
  it("follows the reason loop when present (explicit, not derived)", () => {
    const deck = deckWithLoop();
    expect(deriveStage(deck)).toEqual({ stage: "execute", derived: false, waiting: false, failed: false, status: "active" });

    const reconning = deckWithLoop();
    reconning.reasonLoop.recon = { status: "running", startedAt: 100 };
    expect(deriveStage(reconning).stage).toBe("recon");

    const reasoning = deckWithLoop();
    reasoning.reasonLoop.cycles[0].status = "running";
    expect(deriveStage(reasoning).stage).toBe("reason");

    const done = deckWithLoop();
    done.reasonLoop.stopReason = "budget";
    expect(deriveStage(done).stage).toBe("finalize");
  });

  it("marks paused loops as waiting and runtime failures as failed", () => {
    const deck = deckWithLoop();
    deck.reasonLoop.paused = true;
    expect(deriveStage(deck).waiting).toBe(true);
    expect(deriveStage(deck).status).toBe("paused");
    const failed = deckWithLoop();
    failed.outcomeReason = "runtime_failure";
    expect(deriveStage(failed).failed).toBe(true);
    expect(deriveStage(failed).status).toBe("failed");
  });

  it("derives the status dimension (docs/07 P0-3 two-dim model)", () => {
    // healthy run is active
    expect(deriveStage(deckWithLoop()).status).toBe("active");
    // awaiting operator is waiting (distinct from paused)
    const awaiting = deckWithLoop();
    awaiting.awaitingOperator = "needs env";
    expect(deriveStage(awaiting).status).toBe("waiting");
    // run-level runtime degradation paints degraded until the run finishes
    const degraded = deckWithLoop();
    degraded.runtimeDegraded = [{ engine: "pi-web", reason: "probe timeout" }];
    expect(deriveStage(degraded).status).toBe("degraded");
    degraded.finished = true;
    expect(deriveStage(degraded).status).toBe("completed");
    // solved beats completed
    const solved = deckWithLoop();
    solved.solved = true;
    solved.finished = true;
    expect(deriveStage(solved).status).toBe("solved");
  });

  it("falls back to an approximate stage for legacy sessions", () => {
    const deck = emptyDeck("run-old");
    expect(deriveStage(deck)).toMatchObject({ stage: "queued", derived: true });
    deck.started = true;
    expect(deriveStage(deck)).toMatchObject({ stage: "execute", derived: true });
    deck.finished = true;
    expect(deriveStage(deck)).toMatchObject({ stage: "finalize", derived: true });
  });
});

describe("stage rail", () => {
  it("computes completed / active / pending around the current stage", () => {
    const states = stageRailStates({ stage: "reason", derived: false, waiting: false, failed: false, status: "active" });
    expect(states.map((s) => s.state)).toEqual([
      "completed", "completed", "completed", "active",
      "pending", "pending", "pending", "pending",
    ]);
  });

  it("anchors the first timeline item of each stage", () => {
    const deck = deckWithLoop();
    const items = buildTimeline(deck);
    const anchors = stageAnchors(items);
    expect(anchors.recon).toBe("recon");
    expect(anchors.reason).toBe("cycle:reason-1");
    expect(anchors.dispatch).toBe("dispatch:I-14");
    expect(anchors.finalize).toBeUndefined();
  });
});
