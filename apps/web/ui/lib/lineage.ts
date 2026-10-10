import type {
  BlackboardLineageEdge,
  DeckState,
  FactLifecycleState,
} from "./events";

export type RouteLineageNodeKind =
  | "run"
  | "reason"
  | "intent"
  | "worker"
  | "fact"
  | "dead_end"
  | "route"
  | "branch"
  | "other";

export interface RouteLineageNode {
  id: string;
  kind: RouteLineageNodeKind;
  label: string;
  status?: string;
  /** Human-facing duration for the node when a real event time exists (docs/17 §11.2). */
  durationMs?: number;
  intentId?: string;
  workerId?: string;
  factSeq?: number;
}

export interface RouteLineageProjection {
  explicit: boolean;
  nodes: RouteLineageNode[];
  edges: BlackboardLineageEdge[];
  roots: string[];
  legacyIntentIds: string[];
}

function kindFor(id: string): RouteLineageNodeKind {
  if (id.startsWith("run:")) return "run";
  if (id.startsWith("reason:")) return "reason";
  if (id.startsWith("intent:")) return "intent";
  if (id.startsWith("worker:") || id.startsWith("solver:")) return "worker";
  if (id.startsWith("fact:")) return "fact";
  if (id.startsWith("dead:")) return "dead_end";
  if (id.startsWith("route:")) return "route";
  if (id.startsWith("branch:")) return "branch";
  return "other";
}

function suffix(id: string): string {
  const index = id.indexOf(":");
  return index >= 0 ? id.slice(index + 1) : id;
}

function intentFor(deck: DeckState, id: string) {
  const intentId = suffix(id);
  return deck.blackboard.intents.find((item) => item.id === intentId)
    ?? deck.reasonLoop.cycles.flatMap((cycle) => cycle.intents).find((item) => item.id === intentId);
}

/** Claim/conclude timestamps only exist on the blackboard intent projection. */
function intentTiming(deck: DeckState, id: string): { claimedTs?: number; concludedTs?: number } {
  const intent = deck.blackboard.intents.find((item) => item.id === suffix(id));
  return { claimedTs: intent?.claimedTs, concludedTs: intent?.concludedTs };
}

function nodeFor(deck: DeckState, id: string): RouteLineageNode {
  const kind = kindFor(id);
  if (kind === "run") {
    return { id, kind, label: deck.challengeName || suffix(id) || deck.runId };
  }
  if (kind === "intent") {
    const intent = intentFor(deck, id);
    // Duration only from real timestamps the intent itself carries; an intent with
    // no conclude time has no duration (never inferred from neighbours).
    const { claimedTs, concludedTs } = intentTiming(deck, id);
    const durationMs = claimedTs != null && concludedTs != null
      ? Math.max(0, concludedTs - claimedTs)
      : undefined;
    return {
      id,
      kind,
      label: intent?.goal || suffix(id),
      status: intent?.status,
      durationMs,
      intentId: suffix(id),
    };
  }
  if (kind === "worker") {
    const workerId = suffix(id);
    return { id, kind, label: workerId, workerId, status: deck.lanes[workerId]?.status };
  }
  if (kind === "fact") {
    const factSeq = Number(suffix(id));
    const fact = deck.blackboard.facts.find((item) => item.factSeq === factSeq);
    return {
      id,
      kind,
      label: fact?.summary || fact?.fact || `fact #${suffix(id)}`,
      status: fact?.state as FactLifecycleState | undefined,
      factSeq: Number.isFinite(factSeq) ? factSeq : undefined,
    };
  }
  if (kind === "reason") return { id, kind, label: `REASON ${suffix(id)}` };
  if (kind === "dead_end") return { id, kind, label: `Dead end ${suffix(id)}` };
  if (kind === "route") return { id, kind, label: suffix(id) };
  if (kind === "branch") return { id, kind, label: suffix(id) };
  return { id, kind, label: id };
}

/**
 * Project only explicit append-only lineage edges. It deliberately does not
 * connect nodes from cycle order, event order, or array position. Missing edges
 * remain legacy and are rendered as an audit list by RoutesWorkspace.
 */
export function projectRouteLineage(deck: DeckState): RouteLineageProjection {
  const rawEdges = deck.blackboard.lineageEdges ?? [];
  const edgeByRelation = new Map<string, BlackboardLineageEdge>();
  for (const edge of rawEdges) {
    if (!edge.from || !edge.to || !edge.kind) continue;
    const relation = `${edge.from}\u0000${edge.to}\u0000${edge.kind}`;
    if (!edgeByRelation.has(relation)) edgeByRelation.set(relation, edge);
  }
  const edges = [...edgeByRelation.values()];
  const nodeIds = new Set<string>();
  const incoming = new Set<string>();
  for (const edge of edges) {
    nodeIds.add(edge.from);
    nodeIds.add(edge.to);
    incoming.add(edge.to);
  }
  const nodes = [...nodeIds].sort().map((id) => nodeFor(deck, id));
  const roots = nodes.filter((node) => !incoming.has(node.id)).map((node) => node.id);
  const linkedIntentIds = new Set<string>();
  for (const edge of edges) {
    if (edge.intentId) linkedIntentIds.add(edge.intentId);
    if (edge.from.startsWith("intent:")) linkedIntentIds.add(suffix(edge.from));
    if (edge.to.startsWith("intent:")) linkedIntentIds.add(suffix(edge.to));
  }
  const legacyIntentIds = deck.blackboard.intents
    .map((intent) => intent.id)
    .filter((id) => !linkedIntentIds.has(id));
  return { explicit: edges.length > 0, nodes, edges, roots, legacyIntentIds };
}
