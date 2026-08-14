/**
 * Pheromone finding view model + fold logic (docs/07 §P0-4 / §6.3, Phase 5).
 *
 * The kernel's BoardProjector (dswarm/swarm/projection.py) emits a
 * `blackboard.delta` with `kind: "finding_upserted"`, `actor: "projector"` for
 * every finding it projects onto the experimental Board. The delta carries only
 * the finding's IMMUTABLE pheromone parameters (base / half-life / created_at);
 * the deck computes the current strength with the kernel's half-life formula
 * (dswarm/swarm/board.py `Finding.pheromone`):
 *
 *   strength = base × 2^(-age_sec / half_life_sec)   clamped to [0, 1]
 *
 * Live runs use the wall clock; finished/replayed runs freeze at the run's
 * finishedAt so a paused replay does not keep decaying in real time (§6.3).
 *
 * Pheromone = current activity / scheduling influence ONLY. It is never merged
 * with truth status (verified/candidate/dead-end) or confidence — the three
 * dimensions render independently (§6.3, risk table).
 *
 * Legacy sessions never emit finding_upserted, so `findings` stays empty and
 * every pheromone readout degrades to N/A — never an error.
 *
 * Everything in this module is a pure function (no React, no DOM) so the
 * formula, banding, fold tolerance and sort/filter are unit-testable.
 */

import { EventType, type DSwarmEvent } from "./events";

/** One experimental-Board finding as the deck renders it. Missing pheromone
 *  parameters stay `undefined` — the UI renders those as N/A. */
export interface PheromoneFindingView {
  findingId: string;
  kind: string;
  target: string;
  payload: Record<string, unknown>;
  /** shared_graph event seq this finding was projected from (links to the
   *  blackboard fact with the same factSeq). */
  sourceSeq?: number;
  /** pheromone_base — strength at creation, before decay. */
  base?: number;
  halfLifeSec?: number;
  /** pheromone_created_at parsed to epoch SECONDS (the kernel sends ISO UTC). */
  createdAt?: number;
  experimental: boolean;
}

export type PheromoneBand = "hot" | "warm" | "cool" | "faint" | "na";

const num = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isFinite(v) ? v : undefined;

function isRuntimeInfraFindingText(x: unknown): boolean {
  const s = String(x ?? "").trim().toLowerCase();
  if (!s) return false;
  if (s.includes("worker cli/runtime failed before producing solver output")) return true;
  if (s.includes("profile_incompatible offline eval cannot use custom endpoint profile")) return true;
  if (s.includes("unknown provider") && s.includes("dswarm-worker") && s.includes("--list-models")) return true;
  if (s.includes("endpoint probe failed:") && (s.includes("curl:") || s.includes("requested url returned error"))) return true;
  return false;
}

/** Parse an ISO-8601 UTC timestamp ("…Z") to epoch seconds; undefined on
 *  anything unparseable (missing/garbled field → N/A, never a throw). */
export function parseIsoSec(v: unknown): number | undefined {
  if (typeof v !== "string" || !v) return undefined;
  const ms = Date.parse(v);
  return Number.isFinite(ms) ? ms / 1000 : undefined;
}

/**
 * Fold one event into the findings list. Only `blackboard.delta` events from
 * the projector with kind finding_upserted change the state; everything else
 * returns the SAME array (reference-equal) so React memoization is undisturbed.
 * Upserts by findingId while preserving arrival order.
 */
export function foldFindingUpserted(
  findings: PheromoneFindingView[],
  ev: DSwarmEvent,
): PheromoneFindingView[] {
  if (ev.event_type !== EventType.BLACKBOARD_DELTA) return findings;
  const p = (ev.payload ?? {}) as Record<string, any>;
  if (p.actor !== "projector") return findings;
  const kind = typeof p.kind === "string" && p.kind ? p.kind : p.delta_type;
  if (kind !== "finding_upserted") return findings;

  if (isRuntimeInfraFindingText(p.target) || isRuntimeInfraFindingText(p.payload?.fact)) return findings;

  const sourceSeq = num(p.source_seq);
  const findingId =
    (typeof p.finding_id === "string" && p.finding_id) ||
    (sourceSeq ? `finding:seq-${sourceSeq}` : "");
  if (!findingId) return findings; // no stable key → skip (never throw)

  const row: PheromoneFindingView = {
    findingId,
    kind: typeof p.finding_kind === "string" ? p.finding_kind : "",
    target: typeof p.target === "string" ? p.target : "",
    payload: p.payload && typeof p.payload === "object" ? p.payload : {},
    sourceSeq,
    base: num(p.pheromone_base),
    halfLifeSec: num(p.pheromone_half_life_sec),
    createdAt: parseIsoSec(p.pheromone_created_at),
    experimental: p.experimental !== false,
  };
  const idx = findings.findIndex((f) => f.findingId === findingId);
  if (idx < 0) return [...findings, row];
  return findings.map((f, i) => (i === idx ? { ...f, ...row } : f));
}

/** Age of a finding in seconds (never negative); undefined when createdAt is
 *  unknown. */
export function pheromoneAgeSec(
  f: Pick<PheromoneFindingView, "createdAt">,
  nowSec: number,
): number | undefined {
  if (f.createdAt == null) return undefined;
  return Math.max(0, nowSec - f.createdAt);
}

/**
 * Current pheromone strength — the kernel's exact formula:
 * `base × 2^(-age / half_life)` clamped to [0, 1], age floored at 0,
 * half-life floored at 1s. Returns undefined (→ N/A) when any immutable
 * parameter is missing.
 */
export function pheromoneStrength(
  f: Pick<PheromoneFindingView, "base" | "halfLifeSec" | "createdAt">,
  nowSec: number,
): number | undefined {
  if (f.base == null || f.halfLifeSec == null || f.createdAt == null) return undefined;
  const age = Math.max(0, nowSec - f.createdAt);
  const half = Math.max(1, Math.trunc(f.halfLifeSec));
  const s = f.base * Math.pow(0.5, age / half);
  return Math.max(0, Math.min(1, s));
}

/** §6.3 bands: 0.75–1.00 hot (deep green) / 0.40–0.74 warm (grey-green) /
 *  0.15–0.39 cool (low-saturation green) / <0.15 faint (grey, never hidden). */
export function pheromoneBand(strength: number | undefined): PheromoneBand {
  if (strength == null) return "na";
  if (strength >= 0.75) return "hot";
  if (strength >= 0.4) return "warm";
  if (strength >= 0.15) return "cool";
  return "faint";
}

/** The "now" a strength computation runs against. Live runs use the wall
 *  clock; a FINISHED run (incl. every replayed legacy session) freezes at its
 *  finishedAt so the displayed strength stops decaying once the run ends —
 *  replay uses the virtual (cursor/end) time, not real time (§6.3).
 *  finishedAt arrives as epoch seconds OR milliseconds; normalise. */
export function pheromoneClockSec(
  state: { finished?: boolean; finishedAt?: number },
  nowSec: number,
): number {
  if (state.finished && state.finishedAt) {
    return state.finishedAt < 1e12 ? state.finishedAt : state.finishedAt / 1000;
  }
  return nowSec;
}

/** Compact age/duration label ("45s", "6m", "2h", "3d") for base/half-life/age
 *  readouts. Pure formatting — the i18n layer owns the surrounding words. */
export function formatAgeSec(sec: number): string {
  const s = Math.max(0, Math.round(sec));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

/** Find the finding projected from a given shared_graph fact seq. */
export function findingForFactSeq(
  findings: PheromoneFindingView[],
  factSeq: number | undefined,
): PheromoneFindingView | undefined {
  if (factSeq == null) return undefined;
  return findings.find((f) => f.sourceSeq === factSeq);
}

/** Distinct finding kinds, first-seen order (for the kind filter). */
export function findingKinds(findings: PheromoneFindingView[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const f of findings) {
    if (f.kind && !seen.has(f.kind)) {
      seen.add(f.kind);
      out.push(f.kind);
    }
  }
  return out;
}

/** Strength-descending sort (asc on request); findings without computable
 *  strength (N/A) always sink to the bottom, input order preserved among ties.
 *  Returns a NEW array — the input (deck state) is never mutated. */
export function sortFindingsByStrength(
  findings: PheromoneFindingView[],
  nowSec: number,
  dir: "desc" | "asc" = "desc",
): PheromoneFindingView[] {
  const keyed = findings.map((f, i) => ({ f, i, s: pheromoneStrength(f, nowSec) }));
  keyed.sort((a, b) => {
    if (a.s == null && b.s == null) return a.i - b.i;
    if (a.s == null) return 1;
    if (b.s == null) return -1;
    const d = dir === "desc" ? b.s - a.s : a.s - b.s;
    return d || a.i - b.i;
  });
  return keyed.map((k) => k.f);
}

/** Kind / experimental filtering. `kind` undefined or "" = all kinds.
 *  experimentalOnly keeps only findings the projector flagged experimental. */
export function filterFindings(
  findings: PheromoneFindingView[],
  opts: { kind?: string; experimentalOnly?: boolean } = {},
): PheromoneFindingView[] {
  return findings.filter((f) => {
    if (opts.kind && f.kind !== opts.kind) return false;
    if (opts.experimentalOnly && !f.experimental) return false;
    return true;
  });
}
