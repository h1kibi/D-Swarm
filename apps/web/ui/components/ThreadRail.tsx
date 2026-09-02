"use client";

import { useEffect, useRef, useState } from "react";
import type { CSSProperties, DragEvent as ReactDragEvent, PointerEvent as ReactPointerEvent } from "react";
import { RunSummary, RunStatus, Folder } from "@/lib/useRun";
import { useLang, useT, type Lang } from "@/lib/i18n";
import { Icon, type IconName } from "@/components/Icon";
import { clampRailWidth, railWidthMax, RAIL_WIDTH_DEFAULT, RAIL_WIDTH_MIN, RAIL_WIDTH_MAX } from "@/lib/railSizing";
import {
  FLEET_FILTERS,
  batchTargets,
  filterFleet,
  fleetCounts,
  flagProgress,
  runNeedsAttention,
  sortFleet,
  toggleSelection,
  type BatchAction,
  type FleetFilter,
  type FleetSort,
} from "@/lib/fleet";
import { readKey, writeKey } from "@/lib/storage";

/**
 * Left Run Fleet (docs/07 §5.2) — the thread rail upgraded to a high-density
 * run-management column: status filters (All / Active / Needs Attention /
 * Queued / Paused / Solved / Failed / Archived), a compact row mode, per-row
 * flag progress + HITL-pending badges, and batch pause/resume/stop over a
 * checkbox selection (fanned out to the single-run API by the page; batch stop
 * confirms and never deletes history).
 *
 * Legacy behavior kept: search, folders (drag-and-drop), pin/archive/rename/
 * delete, and the Pinned / Recent / Archived sections. Cleanup shortcuts:
 * double-click a row to rename inline, a hover ⧈ button to archive/unarchive
 * with one click, and a "reapply naming rule" menu item that re-derives
 * `{方向}-{标识}` (题目名 → URL host[:port] → attachment) for sessions
 * dispatched before the rule. Clicking a row only SELECTS it — it never
 * reorders or hoists.
 */

type RailAction =
  | { kind: "pin"; runId: string; pinned: boolean }
  | { kind: "archive"; runId: string; archived: boolean }
  | { kind: "rename"; runId: string; name: string }
  | { kind: "retitle"; runId: string }
  | { kind: "delete"; runId: string }
  | { kind: "move"; runId: string; folderId: string | null }
  | { kind: "newFolder" }
  | { kind: "renameFolder"; folderId: string; name: string }
  | { kind: "deleteFolder"; folderId: string };

export function ThreadRail({
  collapsed,
  width,
  runs,
  folders,
  activeRunId,
  draftActive,
  connected,
  onNew,
  onSelect,
  onAction,
  onResize,
  onOpenSettings,
  onStartupTest,
  onBatch,
}: {
  collapsed: boolean;
  width: number;
  runs: RunSummary[];
  folders: Folder[];
  activeRunId: string;
  /** true when the active conversation is a not-yet-dispatched local draft */
  draftActive: boolean;
  connected: boolean;
  onNew: () => void;
  onSelect: (runId: string) => void;
  // newFolder resolves to the created folder so the rail can start inline-naming
  // it immediately; every other action is fire-and-forget.
  onAction: (a: RailAction) => void | Promise<Folder | null | void>;
  onResize: (width: number) => void;
  onOpenSettings: () => void;
  onStartupTest: () => void;
  // Fleet batch control (§5.2): fan out pause/resume/stop to the selected runs.
  onBatch: (action: BatchAction, runIds: string[]) => void;
}) {
  const t = useT();
  const { lang } = useLang();
  const [showArchived, setShowArchived] = useState(false);
  // Fleet state (§5.2): status filter, within-section sort, compact density,
  // and the batch-selection mode. Compact persists across sessions.
  const [filter, setFilter] = useState<FleetFilter>("all");
  const [sort, setSort] = useState<FleetSort>("attention");
  const [compact, setCompact] = useState(false);
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  useEffect(() => {
    setCompact(readKey("dswarm.fleet.compact") === "1");
  }, []);
  const toggleCompact = () =>
    setCompact((v) => {
      writeKey("dswarm.fleet.compact", v ? "0" : "1");
      return !v;
    });
  const toggleSelect = (runId: string) => setSelected((prev) => toggleSelection(prev, runId));
  const activeRun = runs.find((r) => r.run_id === activeRunId);
  const activeFinished = !draftActive && !!activeRun?.finished;
  // A not-yet-dispatched draft has no backend run, so no SSE stream is opened by
  // design (see useRun). That's "idle", not "disconnected" — only show the red
  // disconnected state when a real, unfinished run has actually lost its stream.
  const footState = connected ? "online" : draftActive || activeFinished ? "idle" : "off";
  const footLabel = connected
    ? t("rail.swarmOnline")
    : activeFinished
      ? t("rail.runFinished")
      : draftActive
        ? t("rail.idle")
        : t("rail.disconnected");
  // Client-side rail search/filter over the already-loaded runs (name / category /
  // status / run_id). Mirrors the search affordance the graph + blackboard already
  // have — the run list is the one busy surface that lacked one. Empty = show all.
  const [query, setQuery] = useState("");
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  // native HTML5 drag-and-drop: the run id being dragged + the current drop zone
  // (folder id, or "" for the top-level Recent group) for the drop highlight.
  const [dragRun, setDragRun] = useState<string | null>(null);
  const [dropZone, setDropZone] = useState<string | null>(null);
  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(new Set());
  const [renamingFolder, setRenamingFolder] = useState<string | null>(null);
  const [resizing, setResizing] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const resizeCleanup = useRef<(() => void) | null>(null);
  // preserve scroll position across run-list refreshes (poll re-renders)
  const scrollTop = useRef(0);
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = scrollTop.current;
  });
  useEffect(() => () => resizeCleanup.current?.(), []);

  const resizeTo = (next: number) => {
    const viewport = typeof window !== "undefined" ? window.innerWidth : undefined;
    onResize(clampRailWidth(next, viewport));
  };

  const startResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (collapsed) return;
    e.preventDefault();
    e.stopPropagation();
    resizeCleanup.current?.();
    setResizing(true);
    document.body.classList.add("rail-resizing");

    const onMove = (ev: PointerEvent) => {
      ev.preventDefault();
      resizeTo(ev.clientX);
    };
    const stop = () => {
      setResizing(false);
      document.body.classList.remove("rail-resizing");
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
      resizeCleanup.current = null;
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
    resizeCleanup.current = stop;
    resizeTo(e.clientX);
  };

  const onResizeKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (collapsed) return;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      resizeTo(width - (e.shiftKey ? 32 : 12));
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      resizeTo(width + (e.shiftKey ? 32 : 12));
    } else if (e.key === "Home") {
      e.preventDefault();
      resizeTo(RAIL_WIDTH_MIN);
    } else if (e.key === "End") {
      e.preventDefault();
      resizeTo(railWidthMax(typeof window !== "undefined" ? window.innerWidth : undefined));
    } else if (e.key === "Enter") {
      e.preventDefault();
      resizeTo(RAIL_WIDTH_DEFAULT);
    }
  };

  // Rail-level Escape: dismiss any open ⋯ menu (row or folder) and cancel an
  // in-progress rename. This is the innermost dismiss layer — RenameInput already
  // stops propagation of its own keydown, so its Esc cancels the rename without
  // reaching here; this handles the case where focus has moved off the field
  // (e.g. menu still open) so a single Esc always clears the rail's transient UI.
  // The settings modal / artifact panel sit on top and handle Esc first (their
  // handlers stop propagation or run before this fires), so this never steals
  // their Esc.
  const hasTransient = menuFor !== null || renaming !== null || renamingFolder !== null;
  useEffect(() => {
    if (!hasTransient) return;
    const onEsc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setMenuFor(null);
      setRenaming(null);
      setRenamingFolder(null);
    };
    window.addEventListener("keydown", onEsc);
    return () => window.removeEventListener("keydown", onEsc);
  }, [hasTransient]);

  // Filter every run by the search query (name / category / localized status /
  // run_id), case-insensitive. Applied up front so Pinned / Folders / Recent /
  // Archived all narrow together. Blank query → identity (show everything).
  const q = query.trim().toLowerCase();
  const matchesQuery = (r: RunSummary): boolean => {
    if (!q) return true;
    const hay = [
      r.name,
      r.category,
      r.status,
      t(`rail.status.${r.status}`),
      relTime(r.updated_at, lang),
      r.run_id,
    ].filter(Boolean).join(" ").toLowerCase();
    return hay.includes(q);
  };
  const matched = q ? runs.filter(matchesQuery) : runs;
  // Fleet filter (§5.2) narrows every section at once; chips show live counts.
  const counts = fleetCounts(matched);
  // a bucket that empties out (runs finished/archived away) loses its chip —
  // fall back to 全部 so the rail isn't stuck on an invisible filter.
  useEffect(() => {
    if (filter !== "all" && counts[filter] === 0) setFilter("all");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, counts.all, counts.active, counts.attention, counts.queued, counts.paused, counts.solved, counts.failed, counts.archived]);
  const scoped = filterFleet(matched, filter);

  const pinned = scoped
    .filter((r) => r.pinned && !r.archived)
    .sort((a, b) => (b.pinned_at ?? 0) - (a.pinned_at ?? 0));
  // Sort: a RUNNING run always floats to the top of its section (the operator
  // wants the currently-solving challenge first), then by creation/drag order.
  const byCreationOrder = (a: RunSummary, b: RunSummary) => {
    const ar = a.status === "running" ? 1 : 0;
    const br = b.status === "running" ? 1 : 0;
    if (ar !== br) return br - ar;
    return (b.order ?? 0) - (a.order ?? 0);
  };
  const liveRuns = scoped.filter((r) => !r.pinned && !r.archived);
  const folderIds = new Set(folders.map((f) => f.id));
  // a run with a folder_id whose folder was deleted falls back to top-level.
  const inFolder = (r: RunSummary, fid: string) => r.folder_id === fid;
  // within-section ordering: the default "attention" floats needs-attention
  // runs above the legacy running-first order; other sorts come from lib/fleet.
  const sectionSort = (items: RunSummary[]): RunSummary[] =>
    sort === "attention"
      ? [...items].sort((a, b) =>
          Number(runNeedsAttention(b)) - Number(runNeedsAttention(a)) || byCreationOrder(a, b))
      : sortFleet(items, sort);
  const ungrouped = sectionSort(
    liveRuns.filter((r) => !r.folder_id || !folderIds.has(r.folder_id)),
  );
  const archived = sectionSort(scoped.filter((r) => r.archived));
  // batch action eligibility comes from lib/fleet (pause: live only, resume:
  // paused only, stop: anything not terminal — never a delete).
  const batchable = (action: BatchAction): string[] => batchTargets(runs, selected, action);
  const runBatch = (action: BatchAction) => {
    const ids = batchable(action);
    if (!ids.length) return;
    if (action === "stop" && !window.confirm(t("fleet.confirmStop", { n: ids.length }))) return;
    onBatch(action, ids);
    setSelected(new Set());
  };
  // When searching, surface archived hits automatically so a match isn't hidden
  // behind the collapsed Archived toggle, and detect a genuinely empty result so
  // we can show a "no matches" state instead of a bare blank rail.
  const searching = q.length > 0;
  const noMatches = searching && pinned.length === 0 && liveRuns.length === 0 && archived.length === 0;

  // drop a dragged run into a folder (fid="" → top-level / un-file).
  const dropInto = (fid: string) => {
    if (dragRun) onAction({ kind: "move", runId: dragRun, folderId: fid || null });
    setDragRun(null);
    setDropZone(null);
  };
  const dzProps = (zone: string) => ({
    onDragOver: (e: ReactDragEvent) => { e.preventDefault(); if (dropZone !== zone) setDropZone(zone); },
    onDragLeave: () => setDropZone((z) => (z === zone ? null : z)),
    onDrop: (e: ReactDragEvent) => { e.preventDefault(); dropInto(zone); },
  });
  const toggleFolder = (fid: string) =>
    setCollapsedFolders((prev) => {
      const next = new Set(prev);
      next.has(fid) ? next.delete(fid) : next.add(fid);
      return next;
    });

  const rowProps = (r: RunSummary) => ({
    run: r,
    active: r.run_id === activeRunId,
    menuOpen: menuFor === r.run_id,
    renaming: renaming === r.run_id,
    compact,
    selectMode,
    selected: selected.has(r.run_id),
    onToggleSelect: () => toggleSelect(r.run_id),
    onSelect: () => onSelect(r.run_id),
    onToggleMenu: () => setMenuFor((cur) => (cur === r.run_id ? null : r.run_id)),
    onCloseMenu: () => setMenuFor(null),
    onStartRename: () => { setMenuFor(null); setRenaming(r.run_id); },
    onCommitRename: (name: string) => {
      setRenaming(null);
      const trimmed = name.trim();
      if (trimmed && trimmed !== r.name) onAction({ kind: "rename", runId: r.run_id, name: trimmed });
    },
    onCancelRename: () => setRenaming(null),
    onAction,
    dragging: dragRun === r.run_id,
    onDragStart: () => setDragRun(r.run_id),
    onDragEnd: () => { setDragRun(null); setDropZone(null); },
  });

  return (
    <nav
      className={`rail motion-shell-piece ${collapsed ? "collapsed" : ""} ${resizing ? "resizing" : ""}`}
      style={{ "--rail-width": `${width}px` } as CSSProperties}
      aria-label={t("a11y.nav")}
    >
      <div className="rail-body">
        <div className="rail-top">
          <span className="brand"><span>D-Swarm</span></span>
        </div>
        <div className="rail-actions">
          <button className="newsolve" onClick={onNew}>{t("rail.newSolve")}</button>
          <button className="newsolve" onClick={onStartupTest}>{t("rail.startupTest")}</button>
        </div>

        <div className="rail-search">
          <span className="rail-search-ico" aria-hidden="true"><Icon name="search" size={14} /></span>
          <input
            className="rail-search-input"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Escape" && query) { e.stopPropagation(); setQuery(""); } }}
            placeholder={t("rail.search")}
            aria-label={t("rail.search")}
          />
          {query && (
            <button
              className="rail-search-clear"
              onClick={() => setQuery("")}
              title={t("rail.searchClear")}
              aria-label={t("rail.searchClear")}
            ><Icon name="x" size={13} /></button>
          )}
        </div>

        <div className="fleet-filters" role="group" aria-label={t("fleet.title")}>
          {FLEET_FILTERS.map((f) => {
            // a filter that matches nothing is dead chrome (with 9 finished
            // sessions the row was 6 zero chips deep) — show only live counts,
            // "全部" always stays as the anchor.
            if (f !== "all" && counts[f] === 0) return null;
            return (
              <button
                key={f}
                type="button"
                className={`fleet-chip ${filter === f ? "on" : ""} ${f === "attention" && counts.attention > 0 ? "has-attn" : ""}`}
                aria-pressed={filter === f}
                onClick={() => setFilter(f)}
              >
                {t(`fleet.filter.${f}`)}
                {counts[f] > 0 && <span className="fleet-chip-n">{counts[f]}</span>}
              </button>
            );
          })}
        </div>

        <div className="fleet-tools">
          <select
            className="fleet-sort"
            value={sort}
            onChange={(e) => setSort(e.target.value as FleetSort)}
            title={t("fleet.sortTitle")}
            aria-label={t("fleet.sortTitle")}
          >
            <option value="attention">{t("fleet.sort.attention")}</option>
            <option value="newest">{t("fleet.sort.newest")}</option>
            <option value="oldest">{t("fleet.sort.oldest")}</option>
            <option value="name">{t("fleet.sort.name")}</option>
          </select>
          <span className="spacer" />
          <button
            type="button"
            className={`fleet-tool ${compact ? "on" : ""}`}
            onClick={toggleCompact}
            title={t(compact ? "fleet.comfort" : "fleet.compact")}
            aria-label={t(compact ? "fleet.comfort" : "fleet.compact")}
            aria-pressed={compact}
          ><Icon name="rows" size={14} /></button>
          <button
            type="button"
            className={`fleet-tool ${selectMode ? "on" : ""}`}
            onClick={() => { setSelectMode((v) => !v); setSelected(new Set()); }}
            title={t(selectMode ? "fleet.selectDone" : "fleet.selectMode")}
            aria-label={t(selectMode ? "fleet.selectDone" : "fleet.selectMode")}
            aria-pressed={selectMode}
          ><Icon name="check" size={14} /></button>
        </div>

        {selectMode && (
          <div className="fleet-batch" role="group" aria-label={t("fleet.selectMode")}>
            <span className="fleet-batch-n">{t("fleet.selected", { n: selected.size })}</span>
            <button type="button" onClick={() => setSelected(new Set(scoped.map((r) => r.run_id)))}>
              {t("fleet.selectAll")}
            </button>
            <button type="button" onClick={() => setSelected(new Set())} disabled={selected.size === 0}>
              {t("fleet.clearSelection")}
            </button>
            <span className="spacer" />
            <button type="button" onClick={() => runBatch("pause")} disabled={batchable("pause").length === 0}>
              {t("fleet.batch.pause")}
            </button>
            <button type="button" onClick={() => runBatch("resume")} disabled={batchable("resume").length === 0}>
              {t("fleet.batch.resume")}
            </button>
            {/* archiving is the fleet's cleanup path: on the archived filter the
              same slot flips to unarchive so the operator can batch-restore. */}
            {filter === "archived" ? (
              <button type="button" onClick={() => runBatch("unarchive")} disabled={batchable("unarchive").length === 0}>
                {t("fleet.batch.unarchive")}
              </button>
            ) : (
              <button type="button" onClick={() => runBatch("archive")} disabled={batchable("archive").length === 0}>
                {t("fleet.batch.archive")}
              </button>
            )}
            <button
              type="button"
              className="danger"
              onClick={() => runBatch("stop")}
              disabled={batchable("stop").length === 0}
            >{t("fleet.batch.stop")}</button>
          </div>
        )}

        <div
          className="rail-scroll"
          ref={scrollRef}
          onScroll={(e) => { scrollTop.current = (e.target as HTMLDivElement).scrollTop; }}
        >
        {draftActive && (
          <>
            <div className="rail-sec">{t("rail.active")}</div>
            <div className="thread">
              <button className="thread-item active is-draft motion-rail-item" onClick={() => onSelect(activeRunId)}>
                <StatusIcon status="draft" t={t} />
                <span className="nm">{t("rail.newSolveItem")}</span>
              </button>
            </div>
          </>
        )}

        {pinned.length > 0 && (
          <>
            <div className="rail-sec">{t("rail.pinned")}</div>
            <div className="thread">
              {pinned.map((r) => <RailRow key={r.run_id} pinned {...rowProps(r)} t={t} lang={lang} />)}
            </div>
          </>
        )}

        {folders.map((f) => {
          const items = sectionSort(liveRuns.filter((r) => inFolder(r, f.id)));
          // While searching, hide folders that contain no match so results aren't
          // buried under empty folder headers. (Drag-and-drop targets are moot mid-search.)
          if (searching && items.length === 0) return null;
          const open = searching || !collapsedFolders.has(f.id);
          return (
            <div
              key={f.id}
              className={`rail-folder ${dropZone === f.id ? "drop" : ""} ${menuFor === `folder:${f.id}` ? "menu-open" : ""}`}
              {...dzProps(f.id)}
            >
              <div className="rail-folder-head">
                <button className="rail-folder-toggle" onClick={() => toggleFolder(f.id)}>
                  <span className="rail-folder-caret">{open ? "▾" : "▸"}</span>
                  {renamingFolder === f.id ? (
                    <RenameInput
                      initial={f.name}
                      onCommit={(name) => { setRenamingFolder(null); const v = name.trim(); if (v && v !== f.name) onAction({ kind: "renameFolder", folderId: f.id, name: v }); }}
                      onCancel={() => setRenamingFolder(null)}
                    />
                  ) : (
                    <span className="rail-folder-name"><Icon name="folder" size={13} /> {f.name}</span>
                  )}
                  <span className="rail-folder-count">{items.length}</span>
                </button>
                <div className="row-menu">
                  <button className="dots" title={t("rail.menu.moreActions")} aria-label={t("rail.menu.moreActions")}
                    onClick={(e) => { e.stopPropagation(); setMenuFor((cur) => (cur === `folder:${f.id}` ? null : `folder:${f.id}`)); }}><Icon name="more" size={15} /></button>
                  {menuFor === `folder:${f.id}` && (
                    <FolderMenu
                      t={t}
                      onClose={() => setMenuFor(null)}
                      onRename={() => { setMenuFor(null); setRenamingFolder(f.id); }}
                      onDelete={() => { setMenuFor(null); onAction({ kind: "deleteFolder", folderId: f.id }); }}
                    />
                  )}
                </div>
              </div>
              {open && (
                <div className="thread">
                  {items.length === 0
                    ? <div className="rail-folder-empty">{t("rail.folderEmpty")}</div>
                    : items.map((r) => <RailRow key={r.run_id} {...rowProps(r)} t={t} lang={lang} />)}
                </div>
              )}
            </div>
          );
        })}

        <div className={`rail-sec rail-recent-head ${dropZone === "" ? "drop" : ""}`} {...dzProps("")}>
          <span>{t("rail.recent")}</span>
          <button className="rail-newfolder" title={t("rail.newFolderTitle")} aria-label={t("rail.newFolderTitle")}
            onClick={async () => {
              const folder = await onAction({ kind: "newFolder" });
              // immediately drop the new folder into inline-rename (auto-focused);
              // blur/Enter commits — type nothing and it just keeps the default name.
              if (folder && typeof folder === "object" && "id" in folder) setRenamingFolder(folder.id);
            }}><Icon name="folderPlus" size={15} /></button>
        </div>
        <div className={`thread ${dropZone === "" ? "drop" : ""}`} {...dzProps("")}>
          {ungrouped.length === 0 && !draftActive && !searching && (
            <div className="rail-empty">
              {t("rail.empty")}
              <span className="rail-empty-hint">{t("rail.emptyHint")}</span>
            </div>
          )}
          {ungrouped.map((r) => <RailRow key={r.run_id} {...rowProps(r)} t={t} lang={lang} />)}
        </div>

        {noMatches && (
          <div className="rail-noresult">
            <span className="rail-noresult-ico" aria-hidden="true"><Icon name="search" size={18} /></span>
            <span className="rail-noresult-title">{t("rail.searchEmpty")}</span>
            <span className="rail-noresult-hint">{t("rail.searchEmptyHint")}</span>
          </div>
        )}

        {archived.length > 0 && (
          // While searching (or on the Archived fleet filter), reveal matching
          // archived runs inline; otherwise keep the manual show/hide.
          searching || filter === "archived" ? (
            <>
              <div className="rail-sec">{t("rail.archived")}</div>
              <div className="thread">
                {archived.map((r) => <RailRow key={r.run_id} archived {...rowProps(r)} t={t} lang={lang} />)}
              </div>
            </>
          ) : (
            <>
              <button
                className="rail-archtoggle"
                title={t("rail.archivedHint")}
                onClick={() => setShowArchived((v) => !v)}
              >
                {showArchived ? t("rail.hideArchived") : `${t("rail.showArchived")} (${archived.length})`}
              </button>
              {showArchived && (
                <>
                  <div className="rail-archhint">{t("rail.archivedHint")}</div>
                  <div className="thread">
                    {archived.map((r) => <RailRow key={r.run_id} archived {...rowProps(r)} t={t} lang={lang} />)}
                  </div>
                </>
              )}
            </>
          )
        )}
        </div>

        <div className="rail-foot">
          <div className="rail-foot-status">
            <span className="rail-foot-state">
              <span className={`dot ${footState === "online" ? "" : footState}`} />
              <span>{footLabel}</span>
            </span>
            <button className="rail-settings-btn" onClick={onOpenSettings} title={t("settings.open")} aria-label={t("settings.open")}>
              <Icon name="gear" size={14} />
            </button>
          </div>
        </div>
      </div>
      {!collapsed && (
        <div
          className="rail-resizer"
          role="separator"
          tabIndex={0}
          aria-label={t("rail.resize")}
          title={t("rail.resize")}
          aria-orientation="vertical"
          aria-valuemin={RAIL_WIDTH_MIN}
          aria-valuemax={RAIL_WIDTH_MAX}
          aria-valuenow={width}
          onPointerDown={startResize}
          onKeyDown={onResizeKey}
          onDoubleClick={() => resizeTo(RAIL_WIDTH_DEFAULT)}
        />
      )}
    </nav>
  );
}

function RailRow({
  run,
  active,
  pinned,
  archived,
  menuOpen,
  renaming,
  compact,
  selectMode,
  selected,
  onToggleSelect,
  onSelect,
  onToggleMenu,
  onCloseMenu,
  onStartRename,
  onCommitRename,
  onCancelRename,
  onAction,
  dragging,
  onDragStart,
  onDragEnd,
  t,
  lang,
}: {
  run: RunSummary;
  active: boolean;
  pinned?: boolean;
  archived?: boolean;
  menuOpen: boolean;
  renaming: boolean;
  compact?: boolean;
  selectMode?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
  onSelect: () => void;
  onToggleMenu: () => void;
  onCloseMenu: () => void;
  onStartRename: () => void;
  onCommitRename: (name: string) => void;
  onCancelRename: () => void;
  onAction: (a: RailAction) => void;
  dragging: boolean;
  onDragStart: () => void;
  onDragEnd: () => void;
  t: (k: string, v?: Record<string, string | number>) => string;
  lang: Lang;
}) {
  const name = run.name || t("rail.newSolveItem");
  const when = relTime(run.updated_at, lang);
  const fp = flagProgress(run);
  const attention = runNeedsAttention(run);
  const cls = [
    "thread-item",
    "motion-rail-item",
    active ? "active" : "",
    pinned ? "is-pinned" : "",
    archived ? "is-archived" : "",
    dragging ? "dragging" : "",
    menuOpen ? "menu-open" : "",
    compact ? "compact" : "",
    attention ? "needs-attention" : "",
  ].filter(Boolean).join(" ");
  const suppressRowDragRef = useRef(false);
  const shouldSuppressRowDrag = (target: EventTarget | null) => {
    if (suppressRowDragRef.current) return true;
    return target instanceof HTMLElement && !!target.closest(".row-menu, .menu, button, input, .rename-input");
  };
  const markMenuPointer = (e: ReactPointerEvent<HTMLDivElement>) => {
    suppressRowDragRef.current = true;
    e.stopPropagation();
  };
  const clearMenuPointerSoon = () => {
    window.setTimeout(() => { suppressRowDragRef.current = false; }, 0);
  };

  return (
    <div
      className={cls}
      role="button"
      tabIndex={0}
      // NOT draggable while renaming OR while the ⋯ menu is open — otherwise a
      // press on a menu item starts an HTML5 row-drag (drag begins on mousedown,
      // before the button's click ever fires) and the option never registers.
      draggable={!renaming && !menuOpen}
      onDragStart={(e) => {
        if (shouldSuppressRowDrag(e.target)) {
          e.preventDefault();
          e.stopPropagation();
          return;
        }
        e.dataTransfer.effectAllowed = "move";
        onDragStart();
      }}
      onDragEnd={() => { suppressRowDragRef.current = false; onDragEnd(); }}
      title={`${name} · ${run.category || "—"} · ${t("rail.rowHint")}`}
      onClick={() => !renaming && onSelect()}
      onDoubleClick={() => { if (!renaming && !selectMode) onStartRename(); }}
      onKeyDown={(e) => { if ((e.key === "Enter" || e.key === " ") && !renaming) { e.preventDefault(); onSelect(); } }}
    >
      <StatusIcon status={run.status} t={t} />
      {selectMode && (
        <input
          type="checkbox"
          className="fleet-check"
          checked={!!selected}
          aria-label={t("fleet.selectRun", { name })}
          onClick={(e) => e.stopPropagation()}
          onChange={() => onToggleSelect?.()}
        />
      )}
      <div className="nm-wrap">
        {renaming ? (
          <RenameInput initial={run.name} onCommit={onCommitRename} onCancel={onCancelRename} />
        ) : (
          <>
            <span className="nm">{name}</span>
            <span className="sub">
              {run.category && <span className="ct">{run.category}</span>}
              <span className="st">{t(`rail.status.${run.status}`)}</span>
              {run.status === "queued" && run.queue_position != null && (
                <span className="st" title={t("rail.status.queued")}>#{run.queue_position}</span>
              )}
              {fp && (
                <span className="st fleet-flags" title={t("fleet.flagsTitle")}>
                  <Icon name="flag" size={11} /> {fp.got}/{fp.need}
                </span>
              )}
              {attention && (
                <span className="fleet-attn" title={t("fleet.hitl")} aria-label={t("fleet.hitl")}>!</span>
              )}
              {when && <span className="when" title={absTime(run.updated_at)}>{when}</span>}
            </span>
          </>
        )}
      </div>

      {pinned && <span className="pin-badge" title={t("rail.menu.unpin")}><Icon name="pin" size={12} /></span>}

      {!renaming && (
        // stop drag from initiating inside the menu region: HTML5 drag starts on
        // mousedown at the draggable row, so a press on the ⋯ button or any menu
        // item would begin a row-drag before the click lands. Halting mousedown +
        // dragstart here keeps presses in the menu as plain clicks.
        <div
          className={`row-menu ${menuOpen ? "menu-open" : ""}`}
          draggable={false}
          onPointerDownCapture={markMenuPointer}
          onPointerUpCapture={clearMenuPointerSoon}
          onPointerCancelCapture={clearMenuPointerSoon}
          onMouseDown={(e) => e.stopPropagation()}
          onDragStart={(e) => { e.preventDefault(); e.stopPropagation(); }}
        >
          {/* one-click archive/unarchive — the fleet's cleanup path shouldn't
            need a menu dig. Same hover-reveal + drag suppression as the ⋯ menu
            (it lives inside .row-menu for exactly that). Hidden mid-selection. */}
          {!selectMode && (
            <button
              className="dots qact"
              title={run.archived ? t("rail.menu.unarchive") : t("rail.menu.archive")}
              aria-label={run.archived ? t("rail.menu.unarchive") : t("rail.menu.archive")}
              onClick={(e) => { e.stopPropagation(); onAction({ kind: "archive", runId: run.run_id, archived: !run.archived }); }}
            ><Icon name="archive" size={14} /></button>
          )}
          <button
            className="dots"
            title={t("rail.menu.moreActions")}
            aria-label={t("rail.menu.moreActions")}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={(e) => { e.stopPropagation(); onToggleMenu(); clearMenuPointerSoon(); }}
          ><Icon name="more" size={15} /></button>
          {menuOpen && (
            <RowMenu
              run={run}
              t={t}
              onClose={onCloseMenu}
              onStartRename={onStartRename}
              onAction={onAction}
            />
          )}
        </div>
      )}
    </div>
  );
}

function tsMs(ts?: number): number {
  if (!ts) return 0;
  return ts < 1e12 ? ts * 1000 : ts;
}

function relTime(ts: number | undefined, lang: Lang): string {
  const ms = tsMs(ts);
  if (!ms) return "";
  const sec = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (sec < 5) return lang === "zh" ? "刚刚" : "just now";
  if (sec < 60) return lang === "zh" ? `${sec} 秒前` : `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return lang === "zh" ? `${min} 分钟前` : `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return lang === "zh" ? `${hr} 小时前` : `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return lang === "zh" ? `${day} 天前` : `${day}d ago`;
}

function absTime(ts: number | undefined): string | undefined {
  const ms = tsMs(ts);
  return ms ? new Date(ms).toLocaleString() : undefined;
}

function RowMenu({
  run, t, onClose, onStartRename, onAction,
}: {
  run: RunSummary;
  t: (k: string, v?: Record<string, string | number>) => string;
  onClose: () => void;
  onStartRename: () => void;
  onAction: (a: RailAction) => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  // rows near the bottom of the rail can't fit the ~190px dropdown below the
  // ⋯ button (the scroll edge shears it) — flip the panel above the row there.
  const [up, setUp] = useState(false);
  useEffect(() => {
    const anchor = ref.current?.parentElement;
    const r = anchor?.getBoundingClientRect();
    if (r && window.innerHeight - r.bottom < 220) setUp(true);
  }, []);
  useEffect(() => {
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) onClose(); };
    const onEsc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onEsc);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onEsc); };
  }, [onClose]);
  // focus the first ENABLED item on open (Share/Collab are disabled placeholders)
  // so the menu is keyboard-driveable the instant it appears.
  useEffect(() => { ref.current?.querySelector<HTMLButtonElement>(".mi:not([disabled])")?.focus(); }, []);

  const act = (a: RailAction) => { onClose(); onAction(a); };

  return (
    <div
      className={`menu ${up ? "up" : ""}`}
      ref={ref}
      onPointerDown={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
    >
      <button className="mi" onClick={onStartRename}>{t("rail.menu.rename")}</button>
      <button
        className="mi"
        title={t("rail.menu.retitleHint")}
        onClick={() => act({ kind: "retitle", runId: run.run_id })}
      >{t("rail.menu.retitle")}</button>
      <div className="msep" />
      {run.pinned ? (
        <button className="mi" onClick={() => act({ kind: "pin", runId: run.run_id, pinned: false })}>{t("rail.menu.unpin")}</button>
      ) : (
        <button className="mi" onClick={() => act({ kind: "pin", runId: run.run_id, pinned: true })}>{t("rail.menu.pin")}</button>
      )}
      {run.archived ? (
        <button className="mi" onClick={() => act({ kind: "archive", runId: run.run_id, archived: false })}>{t("rail.menu.unarchive")}</button>
      ) : (
        <button className="mi" onClick={() => act({ kind: "archive", runId: run.run_id, archived: true })}>{t("rail.menu.archive")}</button>
      )}
      <button className="mi danger" onClick={() => act({ kind: "delete", runId: run.run_id })}>{t("rail.menu.delete")}</button>
    </div>
  );
}

function FolderMenu({
  t, onClose, onRename, onDelete,
}: {
  t: (k: string, v?: Record<string, string | number>) => string;
  onClose: () => void;
  onRename: () => void;
  onDelete: () => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  // same bottom-edge flip as RowMenu — folders can sit anywhere in the rail
  const [up, setUp] = useState(false);
  useEffect(() => {
    const anchor = ref.current?.parentElement;
    const r = anchor?.getBoundingClientRect();
    if (r && window.innerHeight - r.bottom < 160) setUp(true);
  }, []);
  useEffect(() => {
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) onClose(); };
    const onEsc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onEsc);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onEsc); };
  }, [onClose]);
  // focus the first item on open so the menu is immediately keyboard-driveable
  useEffect(() => { ref.current?.querySelector<HTMLButtonElement>(".mi")?.focus(); }, []);
  return (
    <div
      className={`menu ${up ? "up" : ""}`}
      ref={ref}
      onPointerDown={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
    >
      <button className="mi" onClick={onRename}>{t("rail.menu.rename")}</button>
      <button className="mi danger" onClick={onDelete}>{t("rail.folderDelete")}</button>
    </div>
  );
}

function RenameInput({ initial, onCommit, onCancel }: {
  initial: string; onCommit: (v: string) => void; onCancel: () => void;
}) {
  const [v, setV] = useState(initial);
  const ref = useRef<HTMLInputElement | null>(null);
  // Guard against a double commit: pressing Enter calls onCommit → the parent
  // unmounts this input → React fires onBlur during unmount → onCommit again.
  // (Escape→onCancel also unmounts, so the same blur would re-commit a discarded
  // edit.) committedRef makes commit/cancel one-shot.
  const committedRef = useRef(false);
  const commit = (value: string) => {
    if (committedRef.current) return;
    committedRef.current = true;
    onCommit(value);
  };
  const cancel = () => {
    if (committedRef.current) return;
    committedRef.current = true;
    onCancel();
  };
  useEffect(() => { ref.current?.focus(); ref.current?.select(); }, []);
  return (
    <input
      ref={ref}
      className="rename-input"
      value={v}
      onChange={(e) => setV(e.target.value)}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === "Enter") commit(v);
        else if (e.key === "Escape") cancel();
      }}
      onBlur={() => commit(v)}
    />
  );
}

/** Status glyph in front of the title — one per lifecycle state. */
function StatusIcon({ status, t }: { status: RunStatus; t: (k: string) => string }) {
  if (status === "running") {
    return <span className="tk spin" aria-label={t("rail.status.running")}><span className="spinner" /></span>;
  }
  const icon: Record<Exclude<RunStatus, "running">, IconName> = {
    draft: "dot",
    queued: "clock",       // waiting for a scheduler slot (P4)
    paused: "pause",
    solved: "flag",
    finished: "check",
    failed: "alert",
    cancelled: "x",
  };
  return (
    <span className={`tk st-${status}`} aria-label={t(`rail.status.${status}`)}>
      <Icon name={icon[status as Exclude<RunStatus, "running">]} size={13} />
    </span>
  );
}
