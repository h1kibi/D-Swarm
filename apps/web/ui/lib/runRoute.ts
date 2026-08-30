/**
 * Run-path helpers (pure; no window access) — the single source of truth for
 * /run/<id> and the detail-view routes /run/<id>/<view>.
 *
 * Detail views (evidence, workers, btw, ...) are REAL routes opened in their
 * own browser tab: the run deck stays a clean timeline-first page while heavy
 * content gets full-width pages that are bookmarkable and side-by-side
 * comparable (operator feedback: "为什么都挤在一个页面，不能新开页面吗").
 */

/** The artifact/detail views promoted to routes. Order = inspector 面板 tab order. */
export const DETAIL_VIEWS = [
  "evidence",
  "workers",
  "graph",
  "timeline",
  "blackboard",
  "findings",
  "credentials",
  "pocs",
  "routes",
  "directives",
  "btw",
] as const;

export type DetailView = (typeof DETAIL_VIEWS)[number];

const DETAIL_VIEW_SET: ReadonlySet<string> = new Set(DETAIL_VIEWS);

export function isDetailView(view: string): view is DetailView {
  return DETAIL_VIEW_SET.has(view);
}

export interface RunPath {
  runId: string;
  /** present when the path points at a detail view (/run/<id>/<view>). */
  view?: DetailView;
}

/** Parse /run/<id> or /run/<id>/<view>. Returns null for any other path. */
export function parseRunPath(pathname: string): RunPath | null {
  const m = pathname.match(/^\/run\/([^/]+)(?:\/([^/]+))?\/?$/);
  if (!m) return null;
  const runId = decodeURIComponent(m[1]);
  if (!runId) return null;
  if (!m[2]) return { runId };
  if (!isDetailView(m[2])) return null;
  return { runId, view: m[2] };
}

/** The run deck URL (drafts map to "/" — they have no backend row). */
export function deckUrlForRun(id: string): string {
  return id && !id.startsWith("draft-") ? `/run/${encodeURIComponent(id)}` : "/";
}

/** Detail-view URL; falls back to the deck URL for unknown views/drafts. */
export function detailUrlForRun(id: string, view: DetailView): string {
  if (!id || id.startsWith("draft-")) return deckUrlForRun(id);
  return `/run/${encodeURIComponent(id)}/${view}`;
}
