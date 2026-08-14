"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { openStartupTestEvents, startStartupTest, type StartupTestEvent } from "@/lib/useRun";
import { useT } from "@/lib/i18n";
import {
  startupTestAlertHeading,
  startupTestAlertMeta,
  startupTestAlertTone,
  startupTestCheckLabel,
  startupTestEventDetail,
  startupTestEventLabel,
  startupTestEventSubject,
  startupTestEventTime,
  startupTestEventTone,
  startupTestHintDeliveryCopy,
  startupTestModeDescription,
  startupTestModeTitle,
  startupTestStatusLabel,
  startupTestWorkerDetail,
  startupTestWorkerPhaseLabel,
} from "@/lib/startupTestPresentation";

export interface StartupTestPanelProps {
  open: boolean;
  onClose: () => void;
}

type WorkerState = {
  phase: string;
  detail: string;
  ok?: boolean | null;
  status?: string;
  layer?: string;
  blocker?: string;
  backend?: string;
  model?: string;
  account_id?: string;
  binding_kind?: string;
  effective_credential_id?: string;
};

type FlowCheckState = { ok?: boolean | null; detail: string };

function workerTone(ok?: boolean | null) {
  if (ok === true) return "good";
  if (ok === false) return "bad";
  return "running";
}

export function StartupTestPanel({ open, onClose }: StartupTestPanelProps) {
  const t = useT();
  const [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<StartupTestEvent[]>([]);
  const [workers, setWorkers] = useState<Record<string, WorkerState>>({});
  const [summary, setSummary] = useState<StartupTestEvent["summary"] | null>(null);
  const [mode, setMode] = useState<"startup" | "full_flow">("startup");
  const [providerAlerts, setProviderAlerts] = useState<StartupTestEvent[]>([]);
  const [flowChecks, setFlowChecks] = useState<Record<string, FlowCheckState>>({});
  const [error, setError] = useState("");
  const [runSeq, setRunSeq] = useState(0);
  const esRef = useRef<EventSource | null>(null);

  const reset = () => {
    esRef.current?.close();
    esRef.current = null;
    setBusy(false);
    setEvents([]);
    setWorkers({});
    setSummary(null);
    setProviderAlerts([]);
    setFlowChecks({});
    setError("");
  };

  useEffect(() => {
    if (!open) return;
    reset();
    let cancelled = false;

    (async () => {
      try {
        const testId = await startStartupTest({ mode });
        if (cancelled) return;
        setBusy(true);
        const es = await openStartupTestEvents(testId);
        esRef.current = es;
        es.onmessage = (e) => {
          try {
            const ev = JSON.parse(e.data) as StartupTestEvent;
            setEvents((prev) => [...prev, ev]);
            if (ev.type === "worker.phase" && ev.worker_id) {
              setWorkers((prev) => ({
                ...prev,
                [ev.worker_id!]: {
                  phase: ev.phase || "",
                  detail: ev.detail || "",
                  ok: ev.ok,
                  status: ev.status,
                  layer: ev.layer,
                  blocker: ev.blocker,
                  backend: ev.backend,
                  model: ev.model,
                  account_id: ev.account_id || ev.effective_credential_id,
                  binding_kind: ev.binding_kind,
                  effective_credential_id: ev.effective_credential_id,
                },
              }));
            }
            if (ev.type === "provider.error" || ev.type === "provider.batch_alert") {
              setProviderAlerts((prev) => [...prev, ev]);
            }
            if (ev.type === "flow.check" && ev.check_id) {
              setFlowChecks((prev) => ({
                ...prev,
                [ev.check_id!]: { ok: ev.ok, detail: ev.detail || "" },
              }));
            }
            if (ev.type === "test.done") {
              setSummary(ev.summary || null);
              setBusy(false);
              es.close();
            }
          } catch {
            // Ignore malformed frames.
          }
        };
        es.onerror = () => {
          setError("SSE 连接已断开，请重新打开检测蜂群面板再试。");
          setBusy(false);
          es.close();
        };
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "启动测试失败");
          setBusy(false);
        }
      }
    })();

    return () => {
      cancelled = true;
      esRef.current?.close();
      esRef.current = null;
    };
  }, [open, mode, runSeq]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  const rerun = () => setRunSeq((seq) => seq + 1);

  const startedEvent = useMemo(() => events.find((ev) => ev.type === "test.started"), [events]);
  const workerRows = useMemo(() => {
    const map = new Map<string, WorkerState>();
    Object.entries(workers).forEach(([id, row]) => map.set(id, row));
    summary?.results.forEach((row) => map.set(row.worker_id, row));
    return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [summary, workers]);
  const checkRows = useMemo(() => {
    if (summary?.checks?.length) return summary.checks;
    return Object.entries(flowChecks).map(([id, row]) => ({ id, ok: Boolean(row.ok), detail: row.detail }));
  }, [flowChecks, summary]);

  if (!open) return null;

  const totalWorkers = summary?.results.length || workerRows.length || startedEvent?.worker_count || 0;
  const passedWorkers = summary?.passed ?? workerRows.filter(([, row]) => row.ok === true).length;
  const failedWorkers = summary?.failed ?? workerRows.filter(([, row]) => row.ok === false).length;
  const totalChecks = checkRows.length;
  const passedChecks = checkRows.filter((row) => row.ok).length;
  const progress = totalWorkers > 0 ? Math.max(4, Math.round((passedWorkers / totalWorkers) * 100)) : busy ? 10 : 0;
  const statusLabel = startupTestStatusLabel({ busy, passed: passedWorkers, total: totalWorkers, failed: failedWorkers });
  const statusTone = failedWorkers > 0 || error ? "bad" : summary?.ok ? "good" : busy ? "running" : "idle";
  const latestAlerts = providerAlerts.slice(-4);
  const latestEvents = events.slice(-10).reverse();
  const benchmark = summary?.benchmark || startedEvent?.benchmark || "local-smoke";

  return (
    <div className="modal-backdrop btw-backdrop startup-test-backdrop" onMouseDown={(e) => {
      if (e.target === e.currentTarget) onClose();
    }}>
      <div className="btw-drawer startup-test-drawer" role="dialog" aria-modal="true" aria-label={t("startupTest.title")}>
        <div className="btw-head startup-test-head">
          <div className="startup-test-titleblock">
            <span className="startup-test-eyebrow">检测蜂群</span>
            <h2>{startupTestModeTitle(mode)}</h2>
            <p>{startupTestModeDescription(mode)}</p>
            <div className="startup-test-modebar" role="tablist" aria-label="测试模式">
              <button className={mode === "startup" ? "active" : ""} onClick={() => setMode("startup")} disabled={busy}>快速启动</button>
              <button className={mode === "full_flow" ? "active" : ""} onClick={() => setMode("full_flow")} disabled={busy}>完整流程</button>
            </div>
          </div>
          <div className="startup-test-head-actions">
            <span className={`startup-test-status-pill ${statusTone}`}>{statusLabel}</span>
            <button className="startup-test-rerun" onClick={rerun} disabled={busy}>重新检测</button>
            <button className="startup-test-close" onClick={onClose} aria-label={t("startupTest.close")}>
              <span aria-hidden="true">×</span>
              关闭
            </button>
          </div>
        </div>

        <div className="startup-test-body">
          {error && <div className="startup-test-error">{error}</div>}

          <section className="startup-test-hero" aria-label="测试摘要">
            <div className="startup-test-kpis">
              <div className={`startup-test-kpi ${failedWorkers ? "bad" : passedWorkers && passedWorkers === totalWorkers ? "good" : ""}`}>
                <span>Worker</span>
                <b>{passedWorkers}/{totalWorkers || "—"}</b>
                <small>{failedWorkers ? `${failedWorkers} 个失败` : busy ? "正在启动" : "启动覆盖"}</small>
              </div>
              <div className={`startup-test-kpi ${totalChecks && passedChecks === totalChecks ? "good" : ""}`}>
                <span>流程检查</span>
                <b>{passedChecks}/{totalChecks || "—"}</b>
                <small>{mode === "full_flow" ? "Reason / 黑板 / BTW" : "快速模式可跳过"}</small>
              </div>
              <div className={`startup-test-kpi ${latestAlerts.length ? "bad" : "good"}`}>
                <span>LLM 告警</span>
                <b>{providerAlerts.length}</b>
                <small>{providerAlerts.length ? "需要关注" : "暂无异常"}</small>
              </div>
            </div>
            <div className="startup-test-progress-wrap">
              <div className="startup-test-progress-meta">
                <span>启动进度</span>
                <span>{progress}% · {benchmark}</span>
              </div>
              <div className={`startup-test-progress ${statusTone}`} aria-label="启动进度" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100} role="progressbar">
                <i style={{ width: `${progress}%` }} />
              </div>
            </div>
          </section>

          <section className="startup-test-section">
            <div className="startup-test-section-head">
              <div>
                <h3>启用 Worker</h3>
                <p>每张卡片显示启动状态、模型绑定与运行层，减少重复噪音。</p>
              </div>
              {busy && <span className="startup-test-live-dot">实时检测中</span>}
            </div>
            <div className="startup-test-worker-grid">
              {workerRows.map(([workerId, row]) => {
                const tone = workerTone(row.ok);
                const chips = [
                  row.layer,
                  row.backend,
                  row.account_id && `账号 ${row.account_id}`,
                  row.model,
                  row.blocker,
                ].filter(Boolean) as string[];
                const detail = startupTestWorkerDetail(row.detail);
                return (
                  <article className={`startup-test-worker-card ${tone}`} key={workerId}>
                    <div className={`startup-test-status-dot ${tone}`} aria-hidden="true" />
                    <div className="startup-test-worker-main">
                      <div className="startup-test-worker-topline">
                        <b title={workerId}>{workerId}</b>
                        <span>{startupTestWorkerPhaseLabel(row.phase, row.ok)}</span>
                      </div>
                      <p title={detail}>{detail}</p>
                      {chips.length > 0 && (
                        <div className="startup-test-chiprow">
                          {chips.slice(0, 5).map((chip) => <em key={chip} title={chip}>{chip}</em>)}
                        </div>
                      )}
                    </div>
                  </article>
                );
              })}
              {!workerRows.length && (
                <div className="startup-test-empty">
                  {busy ? "正在等待 Worker 上报启动事件…" : "打开后会自动检测所有启用的 Worker。"}
                </div>
              )}
            </div>
          </section>

          {(totalChecks > 0 || mode === "full_flow") && (
            <section className="startup-test-section startup-test-flow">
              <div className="startup-test-section-head">
                <div>
                  <h3>系统工作流</h3>
                  <p>{startupTestHintDeliveryCopy()}</p>
                </div>
              </div>
              <div className="startup-test-check-grid">
                {checkRows.map((check) => (
                  <div className={`startup-test-check ${check.ok ? "good" : "bad"}`} key={check.id}>
                    <span>{check.ok ? "✓" : "!"}</span>
                    <b>{startupTestCheckLabel(check.id)}</b>
                    <small>{check.detail || (check.ok ? "已验证" : "等待结果")}</small>
                  </div>
                ))}
                {!checkRows.length && <div className="startup-test-empty">完整流程开始后会逐项显示 Reason、黑板、BTW、停止、提示与恢复检查。</div>}
              </div>
            </section>
          )}

          <section className="startup-test-section startup-test-alert-zone">
            <div className="startup-test-section-head compact">
              <div>
                <h3>LLM 提供商反馈</h3>
                <p>单点网络波动走自恢复；批量/余额类错误会升格告警。</p>
              </div>
              {!providerAlerts.length && <span className="startup-test-muted-chip">无 LLM 告警</span>}
            </div>
            {latestAlerts.length > 0 && (
              <div className="startup-test-alerts" aria-label="Provider alerts">
                {latestAlerts.map((ev) => (
                  <div className={`startup-test-alert ${startupTestAlertTone(ev)}`} key={`${ev.seq}-${ev.type}`}>
                    <b>{startupTestAlertHeading(ev)}</b>
                    <span>{ev.user_message || ev.raw_message || ev.detail}</span>
                    <small>{startupTestAlertMeta(ev)}</small>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="startup-test-section startup-test-events-panel">
            <div className="startup-test-section-head compact">
              <div>
                <h3>实时事件</h3>
                <p>按状态聚合最近 SSE 事件，便于快速判断卡在哪个阶段。</p>
              </div>
              <span className="startup-test-muted-chip">最近 {latestEvents.length}/{events.length} 条</span>
            </div>
            <div className="startup-test-events" role="list" aria-live="polite" aria-label="实时测试事件">
              {latestEvents.map((ev) => {
                const tone = startupTestEventTone(ev);
                const detail = startupTestEventDetail(ev);
                const subject = startupTestEventSubject(ev);
                return (
                  <article className={`startup-test-event ${tone}`} key={`${ev.seq}-${ev.type}`} role="listitem">
                    <span className={`startup-test-event-dot ${tone}`} aria-hidden="true" />
                    <div className="startup-test-event-main">
                      <div className="startup-test-event-top">
                        <b>{startupTestEventLabel(ev)}</b>
                        <em title={subject}>{subject}</em>
                      </div>
                      <p title={detail}>{detail}</p>
                    </div>
                    <small title={`seq ${ev.seq}`}>{startupTestEventTime(ev)} · #{ev.seq}</small>
                  </article>
                );
              })}
              {!latestEvents.length && <div className="startup-test-events-empty">等待 SSE 事件…</div>}
            </div>
          </section>
        </div>

        <div className="startup-test-footer">
          <span>检测蜂群 · Esc 可关闭</span>
        </div>
      </div>
    </div>
  );
}
