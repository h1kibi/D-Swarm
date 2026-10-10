"use client";

import { useState } from "react";
import type { RunSummary } from "@/lib/useRun";
import { Icon } from "@/components/Icon";
import { useLang } from "@/lib/i18n";
import { targetForRun, type TargetProjection } from "@/lib/targetProjection";

export type TargetRailAction =
  | { kind: "pin"; runId: string; pinned: boolean }
  | { kind: "archive"; runId: string; archived: boolean }
  | { kind: "rename"; runId: string; name: string }
  | { kind: "retitle"; runId: string }
  | { kind: "delete"; runId: string }
  | { kind: "move"; runId: string; folderId: string | null }
  | { kind: "newFolder" }
  | { kind: "renameFolder"; folderId: string; name: string }
  | { kind: "deleteFolder"; folderId: string; }
  | { kind: "renameTarget"; targetId: string; name: string }
  | { kind: "archiveTarget"; targetId: string; archived: boolean }
  | { kind: "detailsTarget"; targetId: string };

function statusLabel(status: TargetProjection["status"], zh: boolean): string {
  const labels: Record<TargetProjection["status"], string> = { active: "ACTIVE", waiting: "WAITING", reason_blocked: "REASON BLOCKED", degraded: "DEGRADED", fallback_active: "FALLBACK ACTIVE", solved: "SOLVED", failed: "REVIEW", archived: "ARCHIVED", idle: "IDLE" };
  return zh ? ({ active: "进行中", waiting: "等待中", reason_blocked: "Reason 已阻断", degraded: "运行已降级", fallback_active: "已启用 fallback", solved: "已解决", failed: "需检查", archived: "已归档", idle: "未开始" } as Record<string, string>)[status] : labels[status];
}
function runLabel(run: RunSummary, zh: boolean): string {
  if (run.solved) return zh ? "已解决" : "SOLVED";
  if (run.awaiting_help) return zh ? "需指导" : "GUIDANCE";
  return zh ? ({ draft: "草稿", queued: "排队中", running: "运行中", paused: "已暂停", solved: "已解决", finished: "已完成", failed: "失败", cancelled: "已取消" } as Record<string, string>)[run.status] : run.status.toUpperCase();
}

export function TargetLibrary({
  collapsed, width, targets, activeRunId, activeTargetId, connected, draftActive, onNew, onSelect, onSelectTarget, onDeleteRun, onAction, onResize, onOpenSettings, onStartupTest,
}: {
  collapsed: boolean; width: number; targets: TargetProjection[]; runs: RunSummary[]; folders: unknown[]; activeRunId: string; activeTargetId?: string | null; draftActive: boolean; connected: boolean;
  onNew: () => void; onSelect: (runId: string) => void; onSelectTarget?: (targetId: string) => void; onDeleteRun?: (run: RunSummary) => void; onAction: (action: TargetRailAction) => void | Promise<unknown>; onResize: (width: number) => void; onOpenSettings: () => void; onStartupTest: () => void; onBatch?: unknown;
}) {
  const [query, setQuery] = useState("");
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [renamingTarget, setRenamingTarget] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const { lang } = useLang();
  const zh = lang === "zh";
  const visible = targets.filter((target) => `${target.name} ${target.category}`.toLowerCase().includes(query.trim().toLowerCase()));
  const activeTarget = targetForRun(targets, activeRunId) || targets.find((target) => target.id === activeTargetId);
  if (collapsed) return null;
  return <aside className="target-library" style={{ width }} aria-label={zh ? "目标列表" : "Target Library"}>
    <div className="target-library-head"><div><div className="eyebrow">D-SWARM / 目标列表</div><h2>{zh ? "目标" : "Targets"}</h2></div><button className="target-icon-button" onClick={onNew} title={zh ? "新建目标" : "New target"}><Icon name="folderPlus" size={16} /></button></div>
    <div className="target-library-actions"><button className="target-new-button" onClick={onNew}><Icon name="target" size={14} /> {zh ? "新建目标" : "New target"}</button><button className="target-icon-button" onClick={onOpenSettings} title={zh ? "运行设置" : "Provider settings"}><Icon name="gear" size={15} /></button></div>
    <label className="target-search"><Icon name="search" size={14} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={zh ? "搜索目标…" : "Search targets…"} /></label>
    <div className="target-library-meta"><span>{visible.length} {zh ? "个目标" : "targets"}</span><span className={`target-connection ${connected ? "online" : "offline"}`}><i />{connected ? (zh ? "在线" : "LIVE") : draftActive ? (zh ? "就绪" : "READY") : (zh ? "离线" : "OFFLINE")}</span></div>
    <div className="target-list">{visible.length === 0 ? <div className="target-empty"><Icon name="target" size={20} /><strong>{zh ? "还没有 Target" : "No targets yet"}</strong><span>{zh ? "创建一个 Target，开始第一次 Run" : "Create a target to start a run"}</span></div> : visible.map((target) => {
      const isMenuOpen = menuFor === target.id;
      const isRenaming = renamingTarget === target.id;
      const commitRename = () => {
        const name = renameValue.trim();
        setRenamingTarget(null);
        setMenuFor(null);
        if (name && name !== target.name) onAction({ kind: "renameTarget", targetId: target.id, name });
      };
      return <section className={`target-card ${activeTarget?.id === target.id ? "selected" : ""}`} key={target.id}>
        <div className="target-card-head">
          {isRenaming ? <input className="target-rename-input" autoFocus value={renameValue} onChange={(event) => setRenameValue(event.target.value)} onBlur={commitRename} onKeyDown={(event) => { if (event.key === "Enter") commitRename(); if (event.key === "Escape") { setRenamingTarget(null); setMenuFor(null); } }} aria-label={zh ? "重命名 Target" : "Rename target"} /> : <button className="target-card-main" onClick={() => onSelectTarget ? onSelectTarget(target.id) : onSelect(target.runs[0]?.run_id || "")}><span className={`target-status-dot ${target.status}`} /><span className="target-card-copy"><strong>{target.name}</strong><span>{target.category.toUpperCase()} · {statusLabel(target.status, zh)}</span></span>{target.needsAttention && <span className="target-attention">!</span>}<span className="target-card-chevron">{target.runs.length}</span></button>}
          <button className="target-card-menu-button" onClick={(event) => { event.stopPropagation(); setMenuFor((current) => current === target.id ? null : target.id); }} aria-label={zh ? "目标操作" : "Target actions"} title={zh ? "更多操作" : "More actions"}><Icon name="more" size={15} /></button>
          {isMenuOpen && <div className="target-card-menu" role="menu">
            <button role="menuitem" onClick={() => { setMenuFor(null); onAction({ kind: "detailsTarget", targetId: target.id }); }}><Icon name="eye" size={13} /> {zh ? "查看详情" : "View details"}</button>
            <button role="menuitem" onClick={() => { setRenameValue(target.name); setRenamingTarget(target.id); setMenuFor(null); }}><Icon name="pencil" size={13} /> {zh ? "重命名" : "Rename"}</button>
            <button role="menuitem" onClick={() => { setMenuFor(null); onAction({ kind: "archiveTarget", targetId: target.id, archived: true }); }}><Icon name="archive" size={13} /> {zh ? "归档目标" : "Archive target"}</button>
          </div>}
        </div>
        <div className="target-card-summary"><span>{target.runs.length} {zh ? "个 Run" : "runs"}</span><span>{target.activeCount} {zh ? "进行中" : "active"}</span><span>{target.flagCount} {zh ? "个 Flag" : "flags"}</span></div>
        <div className="target-run-tree">{target.runs.slice(0, 6).map((run) => <div className="target-run-row-wrap" key={run.run_id}><button className={`target-run-row ${run.run_id === activeRunId ? "active" : ""}`} onClick={() => onSelect(run.run_id)}><span className="tree-branch" /><span className={`run-status-dot ${run.status}`} /><span className="target-run-name">{run.run_id}</span><span className="target-run-status">{runLabel(run, zh)}</span></button>{onDeleteRun && <button type="button" className="target-run-delete" onClick={() => onDeleteRun(run)} aria-label={`${zh ? "删除 Run" : "Delete Run"} ${run.run_id}`} title={zh ? "删除 Run" : "Delete Run"}><Icon name="xCircle" size={12} /></button>}</div>)}</div>
      </section>;
    })}</div>
    <div className="target-library-footer"><button onClick={onStartupTest}><Icon name="radio" size={14} /> {zh ? "运行检查" : "Runtime check"}</button><span>⌘K</span></div>
    <div className="target-resizer" onPointerDown={(event) => { const start = event.clientX; const initial = width; (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId); const move = (e: PointerEvent) => onResize(initial + e.clientX - start); const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); }; window.addEventListener("pointermove", move); window.addEventListener("pointerup", up); }} />
  </aside>;
}

