"use client";

import { useEffect, useState } from "react";
import type { TargetReviewSnapshot } from "@/lib/useTargets";
import { fetchTargetReview } from "@/lib/useTargets";
import type { TargetProjection } from "@/lib/targetProjection";
import { Icon } from "@/components/Icon";
import { bindingSummary } from "@/lib/runBinding";
import { useLang } from "@/lib/i18n";

function value(record: Record<string, unknown> | undefined, ...keys: string[]): string {
  if (!record) return "";
  for (const key of keys) {
    const next = record[key];
    if (typeof next === "string" && next.trim()) return next.trim();
  }
  return "";
}
function formatDate(timestamp?: number | null, zh = false): string {
  if (!timestamp) return "--";
  const date = new Date(timestamp > 1e12 ? timestamp : timestamp * 1000);
  return date.toLocaleString(zh ? "zh-CN" : undefined, { dateStyle: "medium", timeStyle: "short" });
}

function targetStatusLabel(status: TargetProjection["status"], zh: boolean): string {
  if (!zh) return status.toUpperCase();
  return ({ active: "进行中", waiting: "等待中", reason_blocked: "Reason 已阻断", degraded: "运行已降级", fallback_active: "已启用 fallback", solved: "已解决", failed: "需检查", archived: "已归档", idle: "未开始" } as Record<string, string>)[status] || status;
}

function runStatusLabel(status: string, solved: boolean, zh: boolean): string {
  if (solved) return zh ? "已解决" : "SOLVED";
  if (!zh) return status.toUpperCase();
  return ({ draft: "草稿", queued: "排队中", running: "运行中", paused: "已暂停", reason_blocked: "Reason 已阻断", degraded: "运行已降级", fallback_active: "已启用 fallback", solved: "已解决", finished: "已完成", failed: "失败", cancelled: "已取消" } as Record<string, string>)[status] || status;
}

function kindLabel(kind: string, zh: boolean): string {
  if (!zh) return kind.toUpperCase();
  return ({ ctf: "CTF", pentest: "渗透测试" } as Record<string, string>)[kind] || kind.toUpperCase();
}

function categoryLabel(category: string, zh: boolean): string {
  if (!zh) return category.toUpperCase();
  return ({ web: "Web", pwn: "Pwn", crypto: "Crypto", reverse: "Reverse", misc: "其他" } as Record<string, string>)[category] || category;
}

export function TargetDetailsModal({ target, onClose, onSelectRun, onDeleteRun, onOpenFact, onPatch, onAddHint, onAddDirection, onAuditReview }: {
  target: TargetProjection;
  onClose: () => void;
  onSelectRun: (runId: string) => void;
  onDeleteRun: (run: TargetProjection["runs"][number]) => void;
  onOpenFact?: (runId: string, factSeq?: number) => void;
  onPatch?: (changes: Record<string, unknown>) => Promise<boolean>;
  onAddHint?: (text: string) => Promise<boolean>;
  onAddDirection?: (label: string, description?: string, category?: string) => Promise<boolean>;
  onAuditReview?: (items: Array<Record<string, unknown>>) => Promise<boolean>;
}) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const copy = zh ? {
    eyebrow: "目标详情", edit: "编辑目标", cancelEdit: "取消编辑", close: "关闭", context: "上下文", name: "名称", mode: "模式", category: "类型", description: "描述", address: "目标地址", flagHint: "Flag 格式提示", flags: "Flag", multiFlag: "多 Flag 目标", expectedFlags: "目标数量", save: "保存信息", saving: "保存中…", noDescription: "暂无描述", notSet: "未设置", singleFlag: "单个 Flag", expected: "个目标", lastActivity: "最近活动", directions: "方向", directionSubtitle: "为新建 Run 选择默认方向。", primary: "主方向", makePrimary: "设为主方向", noDirections: "还没有保存方向。", addDirection: "添加方向", directionPlaceholder: "添加一个方向", directionDescription: "可选说明", hints: "提示", operator: "操作员", noHints: "还没有保存提示。", hintPlaceholder: "为后续 Run 添加操作提示…", addHint: "添加提示", runHistory: "Run 历史", runs: "个 Run", noRuns: "该 Target 还没有 Run，可从工作区启动。", verifiedFacts: "已验证事实", noFacts: "只有从已验证 Run 证据投影后，事实才会显示在这里。", activity: "活动", noActivity: "Target 与 Run 的生命周期活动会显示在这里。", noFact: "已验证事实", sourceRun: "来源 Run", event: "事件", pendingProjection: "等待 provenance 来源投影", inheritedBy: "已被继承到", run: "个 Run", openEvidence: "打开来源 Run 证据", activityItem: "活动", derivedEvidence: "来源于已验证 Run 证据", targetCard: "Target 卡片", errorEmptyName: "Target 名称不能为空。", saveFailed: "Target 信息保存失败，请重试。", hintFailed: "提示保存失败，请重试。", directionFailed: "方向保存失败，请重试。", primaryFailed: "主方向设置失败，请重试。"
  } : {
    eyebrow: "TARGET DETAILS", edit: "Edit target", cancelEdit: "Cancel edit", close: "Close", context: "Context", name: "Name", mode: "Mode", category: "Category", description: "Description", address: "Target address", flagHint: "Flag format hint", flags: "Flags", multiFlag: "Multi-flag target", expectedFlags: "Expected flags", save: "Save metadata", saving: "Saving…", noDescription: "No description", notSet: "Not set", singleFlag: "Single flag", expected: "expected", lastActivity: "Last activity", directions: "Directions", directionSubtitle: "Choose the default branch for new Runs.", primary: "PRIMARY", makePrimary: "Make primary", noDirections: "No exploration directions saved.", addDirection: "Add direction", directionPlaceholder: "Add a direction", directionDescription: "Optional description", hints: "Hints", operator: "operator", noHints: "No hints saved.", hintPlaceholder: "Add an operator hint for future Runs…", addHint: "Add hint", runHistory: "Run history", runs: "runs", noRuns: "This Target has no runs yet. Start one from the workspace.", verifiedFacts: "Verified facts", noFacts: "Facts appear here only after they are projected from verified Run evidence.", activity: "Activity", noActivity: "Target and Run lifecycle activity will appear here.", noFact: "Verified fact", sourceRun: "Source run", event: "event", pendingProjection: "Provenance source pending projection", inheritedBy: "Inherited by", run: "run", openEvidence: "Open source run evidence", activityItem: "Activity", derivedEvidence: "Derived from verified Run evidence", targetCard: "Target card", errorEmptyName: "Target name cannot be empty.", saveFailed: "Could not save Target metadata. Try again.", hintFailed: "Could not save this hint. Try again.", directionFailed: "Could not save this direction. Try again.", primaryFailed: "Could not set the primary direction. Try again."
  };
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [hintSaving, setHintSaving] = useState(false);
  const [error, setError] = useState("");
  const [hintText, setHintText] = useState("");
  const [directionLabel, setDirectionLabel] = useState("");
  const [directionDescription, setDirectionDescription] = useState("");
  const [directionSaving, setDirectionSaving] = useState(false);
  const [name, setName] = useState(target.name);
  const [kind, setKind] = useState(target.kind || "ctf");
  const [category, setCategory] = useState(target.category || "misc");
  const [description, setDescription] = useState(target.description || "");
  const [address, setAddress] = useState(target.targetAddress || "");
  const [flagFormatHint, setFlagFormatHint] = useState(target.flagFormatHint || "");
  const [multiFlag, setMultiFlag] = useState(Boolean(target.multiFlag));
  const [expectedFlags, setExpectedFlags] = useState(target.expectedFlags || 1);
  const [auditSelection, setAuditSelection] = useState<Set<string>>(new Set());
  const [summaryApproved, setSummaryApproved] = useState(false);
  const [auditSaving, setAuditSaving] = useState(false);
  const [reviewSnapshot, setReviewSnapshot] = useState<TargetReviewSnapshot | null>(null);
  useEffect(() => {
    let alive = true;
    void fetchTargetReview(target.id).then((snapshot) => { if (alive) setReviewSnapshot(snapshot); });
    return () => { alive = false; };
  }, [target.id]);

  useEffect(() => {
    setName(target.name);
    setKind(target.kind || "ctf");
    setCategory(target.category || "misc");
    setDescription(target.description || "");
    setAddress(target.targetAddress || "");
    setFlagFormatHint(target.flagFormatHint || "");
    setMultiFlag(Boolean(target.multiFlag));
    setExpectedFlags(target.expectedFlags || 1);
    setEditing(false);
    setError("");
  }, [target.id, target.name, target.kind, target.category, target.description, target.targetAddress, target.flagFormatHint, target.multiFlag, target.expectedFlags]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const saveMetadata = async () => {
    const nextName = name.trim();
    if (!nextName) {
      setError(copy.errorEmptyName);
      return;
    }
    if (!onPatch) {
      setEditing(false);
      return;
    }
    setSaving(true);
    setError("");
    const ok = await onPatch({
      name: nextName,
      kind,
      category,
      description: description.trim(),
      target_address: address.trim(),
      flag_format_hint: flagFormatHint.trim(),
      multi_flag: multiFlag,
      expected_flags: multiFlag ? Math.max(1, expectedFlags) : 1,
    });
    setSaving(false);
    if (ok) setEditing(false);
    else setError(copy.saveFailed);
  };

  const addHint = async () => {
    const nextHint = hintText.trim();
    if (!nextHint || !onAddHint) return;
    setHintSaving(true);
    setError("");
    const ok = await onAddHint(nextHint);
    setHintSaving(false);
    if (ok) setHintText("");
    else setError(copy.hintFailed);
  };

  const addDirection = async () => {
    const nextLabel = directionLabel.trim();
    if (!nextLabel || !onAddDirection) return;
    setDirectionSaving(true);
    setError("");
    const ok = await onAddDirection(nextLabel, directionDescription.trim());
    setDirectionSaving(false);
    if (ok) {
      setDirectionLabel("");
      setDirectionDescription("");
    } else setError(copy.directionFailed);
  };

  const setPrimaryDirection = async (directionId: string) => {
    if (!onPatch || !directionId || directionId === target.primaryDirectionId) return;
    setSaving(true);
    setError("");
    const ok = await onPatch({ primary_direction_id: directionId });
    setSaving(false);
    if (!ok) setError(copy.primaryFailed);
  };

  const auditSelected = async () => {
    const selected = reviewSnapshot ? [
      ...(summaryApproved ? [{ ...reviewSnapshot.summary, approval: "approved" }] : []),
      ...reviewSnapshot.known_facts,
      ...reviewSnapshot.attempted_routes,
    ].filter((item) => auditSelection.has(String(item.item_id || "")) || (item.item_type === "summary" && summaryApproved)) : [];
    const invalid = selected.some((item) => item.item_type === "summary"
      ? !item.source_run_id
      : item.item_type === "route_constraint"
        ? !item.source_run_id || !item.route_hash || !item.seq
        : item.status !== "confirmed" || !item.source_run_id || (!item.fact_seq && !item.dead_end_seq));
    if (!selected.length || invalid || !onAuditReview) return;
    const message = zh ? `确认将 ${selected.length} 条已确认内容写入 Target 长期知识？` : `Persist ${selected.length} confirmed review items to Target knowledge?`;
    if (!window.confirm(message)) return;
    setAuditSaving(true);
    setError("");
    const ok = await onAuditReview(selected);
    setAuditSaving(false);
    if (ok) { setAuditSelection(new Set()); setSummaryApproved(false); }
    else setError(zh ? "审计写入失败，请重试。" : "Audit write failed. Try again.");
  };

  return (
    <div className="target-details-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="target-details-modal" role="dialog" aria-modal="true" aria-labelledby="target-details-title">
        <header className="target-details-header">
          <div><span className="eyebrow">{copy.eyebrow}</span><h2 id="target-details-title">{target.name}</h2><p>{categoryLabel(target.category, zh)} · {targetStatusLabel(target.status, zh)}</p></div>
          <div className="target-details-header-actions">
            {onPatch && <button className="target-ghost-button" onClick={() => { setEditing((current) => !current); setError(""); }}><Icon name="pencil" size={13} />{editing ? copy.cancelEdit : copy.edit}</button>}
            <button className="target-details-close" onClick={onClose} aria-label={copy.close}><Icon name="x" size={17} /></button>
          </div>
        </header>
        <div className="target-details-body">
          <section className="target-details-section target-details-context"><div className="target-details-section-head"><h3>{copy.context}</h3><span>{target.id}</span></div>
            {editing ? <div className="target-details-edit-form">
              <label><span>{copy.name}</span><input value={name} onChange={(event) => setName(event.target.value)} autoFocus /></label>
              <div className="target-details-edit-grid"><label><span>{copy.mode}</span><select value={kind} onChange={(event) => setKind(event.target.value)}><option value="ctf">CTF</option><option value="pentest">{zh ? "渗透测试" : "Pentest"}</option></select></label><label><span>{copy.category}</span><select value={category} onChange={(event) => setCategory(event.target.value)}><option value="web">Web</option><option value="pwn">Pwn</option><option value="crypto">Crypto</option><option value="reverse">Reverse</option><option value="misc">{zh ? "其他" : "Misc"}</option></select></label></div>
              <label><span>{copy.description}</span><textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={4} /></label>
              <div className="target-details-edit-grid"><label><span>{copy.address}</span><input value={address} onChange={(event) => setAddress(event.target.value)} placeholder={zh ? "主机、URL 或题目端点" : "host, URL, or challenge endpoint"} /></label><label><span>{copy.flagHint}</span><input value={flagFormatHint} onChange={(event) => setFlagFormatHint(event.target.value)} placeholder={zh ? "可选的操作员上下文" : "Optional operator context"} /></label></div>
              <div className="target-details-flag-settings"><label><input type="checkbox" checked={multiFlag} onChange={(event) => setMultiFlag(event.target.checked)} /> {copy.multiFlag}</label>{multiFlag && <label><span>{copy.expectedFlags}</span><input type="number" min={1} max={100} value={expectedFlags} onChange={(event) => setExpectedFlags(Math.max(1, Number(event.target.value) || 1))} /></label>}</div>
              <div className="target-details-edit-actions"><button className="target-primary-button" onClick={() => void saveMetadata()} disabled={saving}>{saving ? copy.saving : copy.save}</button>{error && <span className="target-details-error">{error}</span>}</div>
            </div> : <dl className="target-details-facts">
              <div><dt>{copy.description}</dt><dd>{target.description || copy.noDescription}</dd></div>
              <div><dt>{copy.address}</dt><dd className="target-mono">{target.targetAddress || copy.notSet}</dd></div>
              <div><dt>{copy.mode}</dt><dd>{kindLabel(target.kind || "ctf", zh)} · {categoryLabel(target.category, zh)}</dd></div>
              <div><dt>{copy.flags}</dt><dd>{target.multiFlag ? `${target.expectedFlags || 1} ${copy.expected}` : copy.singleFlag}</dd></div>
            </dl>}</section>
          <section className="target-details-section target-review-snapshot" aria-label={zh ? "Run 回顾" : "Run review snapshot"}>
            <div className="target-details-section-head"><div><h3>{zh ? "Run 回顾" : "RUN REVIEW"}</h3><p className="target-details-section-subtitle">{zh ? "仅显示服务端从终止 Run graph 生成的只读快照。" : "Read-only snapshot built server-side from terminated Run graphs."}</p></div><span>{reviewSnapshot?.summary?.terminal_run_count || 0} {zh ? "个已终止 Run" : "terminated Runs"}</span></div>
            <div className="target-review-grid">
              <article className="target-review-card"><strong>{zh ? "总结" : "Summary"}</strong><p>{reviewSnapshot?.summary.text || (zh ? "暂无经审计总结。" : "No audited summary is available.")}</p><label className="target-audit-check"><input type="checkbox" checked={summaryApproved} onChange={(event) => setSummaryApproved(event.target.checked)} disabled={!reviewSnapshot || reviewSnapshot.summary.status !== "operator_approval_required"} /><span>{zh ? "批准此总结" : "Approve summary"}</span></label><small>{reviewSnapshot ? `${reviewSnapshot.summary.run_count} ${zh ? "个关联 Run" : "linked Runs"}` : (zh ? "正在读取…" : "Loading…")}</small></article>
              <article className="target-review-card"><strong>{zh ? "已知事实与候选" : "Known facts & candidates"}</strong>{reviewSnapshot?.known_facts.length ? <ul>{reviewSnapshot.known_facts.slice(-8).map((fact, index) => <li key={`${String(fact.item_id || fact.run_id || "")}-${String(fact.fact_seq || index)}`}>{fact.status === "confirmed" && onAuditReview && <label className="target-audit-check"><input type="checkbox" checked={auditSelection.has(String(fact.item_id))} onChange={() => setAuditSelection((previous) => { const next = new Set(previous); const key = String(fact.item_id); if (next.has(key)) next.delete(key); else next.add(key); return next; })} /><span>{zh ? "审计" : "Audit"}</span></label>}{String(fact.text || "")} · {String(fact.status || "")}</li>)}</ul> : <p>{reviewSnapshot ? copy.noFacts : (zh ? "正在读取…" : "Loading…")}</p>}</article>
              <article className="target-review-card"><strong>{zh ? "尝试过的路线与 Intent" : "Routes & Intents"}</strong>{reviewSnapshot?.attempted_routes.length || reviewSnapshot?.intent_decisions.length ? <ul>{[...(reviewSnapshot?.attempted_routes || []), ...(reviewSnapshot?.intent_decisions || [])].slice(-8).map((route, index) => <li key={`${String(route.item_id || route.intent_id || route.run_id || "")}-${index}`}>{route.status === "confirmed" && (route.item_type === "dead_end" || route.item_type === "route_constraint") && onAuditReview && <label className="target-audit-check"><input type="checkbox" checked={auditSelection.has(String(route.item_id))} onChange={() => setAuditSelection((previous) => { const next = new Set(previous); const key = String(route.item_id); if (next.has(key)) next.delete(key); else next.add(key); return next; })} /><span>{zh ? "审计" : "Audit"}</span></label>}{String(route.text || route.reason || route.decision || route.kind || "route")} · {String(route.status || route.decision || "")}</li>)}</ul> : <p>{reviewSnapshot ? copy.noDirections : (zh ? "正在读取…" : "Loading…")}</p>}</article>
              <article className="target-review-card runtime-only"><strong>{zh ? "失败与运行时问题" : "Failures & runtime"}</strong>{reviewSnapshot?.failures_runtime.length ? <ul>{reviewSnapshot.failures_runtime.slice(-8).map((failure, index) => <li key={`${String(failure.run_id || "")}-${index}`}>{String(failure.run_id || "")}: {String(failure.text || failure.kind || "runtime failure")}</li>)}</ul> : <p>{reviewSnapshot ? (zh ? "没有已记录的运行时失败。" : "No recorded runtime failures.") : (zh ? "正在读取…" : "Loading…")}</p>}<small>{zh ? "runtime-only，不可直接写入 Target 事实" : "runtime-only; never written as a Target fact"}</small></article>
              <article className="target-review-card"><strong>{zh ? "证据与账本" : "Evidence & ledger"}</strong>{reviewSnapshot?.evidence_refs.length || reviewSnapshot?.usage_ledger.length || reviewSnapshot?.provenance_chain.length ? <ul>{[...(reviewSnapshot?.evidence_refs || []), ...(reviewSnapshot?.provenance_chain || []), ...(reviewSnapshot?.usage_ledger || [])].slice(-8).map((item, index) => <li key={`${String(item.run_id || "")}-${String(item.seq || item.usage_id || index)}`}><span className="target-mono">{String(item.run_id || item.usage_id || "")}</span> · {String(item.kind || item.status || item.role || "evidence")} · {Array.isArray(item.artifact_ids) ? item.artifact_ids.join(", ") : String(item.artifact_id || "")}</li>)}</ul> : <p>{reviewSnapshot ? (zh ? "暂无证据引用。" : "No evidence references.") : (zh ? "正在读取…" : "Loading…")}</p>}</article>
            </div>
            {onAuditReview && (auditSelection.size > 0 || summaryApproved) && <button type="button" className="target-primary-button" onClick={() => void auditSelected()} disabled={auditSaving}>{auditSaving ? copy.saving : (zh ? `审计写入 (${auditSelection.size + (summaryApproved ? 1 : 0)})` : `Audit selected (${auditSelection.size + (summaryApproved ? 1 : 0)})`)}</button>}
          </section>
          <div className="target-details-columns">
          <section className="target-details-section target-details-verified"><div className="target-details-section-head"><div><h3>{copy.verifiedFacts}</h3><span>{target.verifiedFacts?.length ?? 0}</span></div></div>
            {target.verifiedFacts?.length ? <div className="target-details-list">{target.verifiedFacts.map((fact, index) => {
              const factKey = String(fact.fact_id || fact.id || fact.fact_seq || index);
              const sourceRunId = value(fact, "source_run_id", "sourceRunId");
              const factSeq = Number(fact.fact_seq);
              const factContent = <><Icon name="check" size={14} /><div><strong>{value(fact, "fact", "statement", "text") || copy.noFact}</strong><span>{sourceRunId ? `${copy.sourceRun}: ${sourceRunId}${Number.isFinite(factSeq) ? ` · ${copy.event} #${factSeq}` : ""}` : copy.pendingProjection}</span></div>{sourceRunId && <Icon name="chevronRight" size={13} />}</>;
              return <div className="target-details-fact-row" key={factKey}>{sourceRunId && onOpenFact ? <button type="button" className="target-details-fact target-details-fact-button" onClick={() => onOpenFact(sourceRunId, Number.isFinite(factSeq) ? factSeq : undefined)} title={copy.openEvidence}>{factContent}</button> : <div className="target-details-fact">{factContent}</div>}</div>;
            })}</div> : <p className="target-details-empty">{copy.noFacts}</p>}
          </section>
            <section className="target-details-section"><div className="target-details-section-head"><h3>{copy.hints}</h3><span>{target.hints?.length ?? 0}</span></div>
              {target.hints?.length ? <div className="target-details-list">{target.hints.map((hint, index) => <div className="target-details-list-row" key={String(hint.id || index)}><Icon name="help" size={14} /><div><strong>{value(hint, "text") || copy.hints}</strong><span>{value(hint, "source") || copy.operator}</span></div></div>)}</div> : <p className="target-details-empty">{copy.noHints}</p>}
              {onAddHint && <div className="target-details-hint-form"><input value={hintText} onChange={(event) => setHintText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void addHint(); } }} placeholder={copy.hintPlaceholder} aria-label={copy.hints} /><button className="target-ghost-button" onClick={() => void addHint()} disabled={!hintText.trim() || hintSaving}>{hintSaving ? copy.saving : copy.addHint}</button></div>}
            </section>
          </div>
          <section className="target-details-section target-details-run-history"><div className="target-details-section-head"><h3>{copy.runHistory}</h3><span>{target.runs.length} {copy.runs}</span></div>
            {target.runs.length ? <div className="target-details-runs">{target.runs.map((run) => <article key={run.run_id} className="target-details-run-card"><button type="button" className="target-details-run" onClick={() => { onClose(); onSelectRun(run.run_id); }}><span className={`run-status-dot ${run.status}`} /><span className="target-mono">{run.run_id}</span><span>{run.direction || run.category || (zh ? "综合探索" : "General")}</span><span className="target-details-binding" title={bindingSummary(run.binding_snapshot, "reason")}>{bindingSummary(run.binding_snapshot, "reason")}</span><em>{runStatusLabel(run.status, run.solved, zh)}</em><Icon name="chevronRight" size={13} /></button><button type="button" className="target-details-run-delete" onClick={() => onDeleteRun(run)} aria-label={`${zh ? "删除 Run" : "Delete Run"} ${run.run_id}`} title={zh ? "删除 Run" : "Delete Run"}><Icon name="xCircle" size={13} /></button></article>)}</div> : <p className="target-details-empty">{copy.noRuns}</p>}
          </section>
          <section className="target-details-section target-details-activity-section"><div className="target-details-section-head"><h3>{copy.activity}</h3><span>{target.activity?.length ?? 0}</span></div>
            {target.activity?.length ? <div className="target-details-activity">{target.activity.slice(0, 40).map((item, index) => <div className="target-details-activity-row" key={String(item.id || index)}><span className={`activity-source-dot ${item.source === "run_projection" ? "projection" : item.source === "run" ? "run" : "target"}`} /><div><strong>{value(item, "label", "kind") || copy.activityItem}</strong><span>{item.run_id ? `${zh ? "Run" : "Run"} ${String(item.run_id)}` : item.source === "run_projection" ? copy.derivedEvidence : copy.targetCard}</span></div><time>{formatDate(Number(item.timestamp || 0), zh)}</time></div>)}</div> : <p className="target-details-empty">{copy.noActivity}</p>}
          </section>
        </div>
      </section>
    </div>
  );
}
