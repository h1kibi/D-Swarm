export type StartupTestAlertLike = {
  type?: string;
  severity?: string;
  retryable?: boolean | null;
  should_pause_dispatch?: boolean | null;
  provider?: string;
  account_id?: string;
  category?: string;
};

export type StartupTestMode = "startup" | "full_flow" | string;

export type StartupTestStatusInput = {
  busy: boolean;
  passed: number;
  total: number;
  failed: number;
};

export function startupTestAlertHeading(ev: StartupTestAlertLike): string {
  return ev.type === "provider.batch_alert" ? "批量 LLM 错误" : "LLM 错误";
}

export function startupTestAlertTone(ev: StartupTestAlertLike): "bad" | "warn" {
  return ev.severity === "fatal" || Boolean(ev.should_pause_dispatch) ? "bad" : "warn";
}

export function startupTestAlertMeta(ev: StartupTestAlertLike): string {
  return [
    ev.provider,
    ev.account_id && `account=${ev.account_id}`,
    ev.category,
    ev.retryable ? "可自动恢复/重试" : "需要处理",
    ev.should_pause_dispatch && "建议暂停派发",
  ].filter(Boolean).join(" · ");
}

export function startupTestHintDeliveryCopy(): string {
  return "普通提示会立即写入黑板 directive；当前 single-shot worker 可不中断，下一个 Worker/intent 会消费，UI 会明确显示投递状态。";
}

export function startupTestModeTitle(mode: StartupTestMode): string {
  return mode === "full_flow" ? "完整流程演练" : "快速启动测试";
}

export function startupTestModeDescription(mode: StartupTestMode): string {
  if (mode === "full_flow") {
    return "使用 local-smoke benchmark 调用真实 LLM，检查所有启用 Worker 以及 Reason、黑板、BTW、停止、提示、恢复与错误韧性。";
  }
  return "快速验证启用 Worker 的凭据、运行环境、模型绑定与启动链路。";
}

const PHASE_LABELS: Record<string, string> = {
  queued: "排队中",
  preparing: "准备中",
  preflight: "预检",
  launching: "启动中",
  starting: "启动中",
  running: "运行中",
  probing: "探测中",
  checking: "检查中",
  cleanup: "清理中",
  teardown: "收尾中",
  done: "已完成",
  failed: "失败",
  error: "异常",
  cancelled: "已取消",
};

export function startupTestWorkerPhaseLabel(phase?: string, ok?: boolean | null): string {
  if (ok === true) return "已通过";
  if (ok === false) return "失败";
  return PHASE_LABELS[(phase || "").toLowerCase()] || phase || "等待中";
}

const DETAIL_LABELS: Record<string, string> = {
  startup_test_ok: "启动链路已验证",
  full_flow_ok: "完整流程已验证",
  benchmark_loaded: "Benchmark 已加载",
  blackboard_ok: "知识黑板读写正常",
  reason_ok: "Reason 意图规划正常",
  btw_ok: "BTW 进展汇报正常",
  stop_ok: "停止与 Worker 收尾正常",
  hint_ok: "提示已写入 directive",
  resume_ok: "会话恢复链路正常",
};

export function startupTestWorkerDetail(detail?: string): string {
  if (!detail) return "等待测试事件";
  return DETAIL_LABELS[detail] || detail.replaceAll("_", " ");
}

const CHECK_LABELS: Record<string, string> = {
  "benchmark.loaded": "Benchmark",
  "workers.checked": "Worker 覆盖",
  "blackboard.checked": "知识黑板",
  "reason.checked": "Reason / 意图",
  "btw.checked": "BTW 汇报",
  "hint.checked": "提示投递",
  "stop.checked": "停止收尾",
  "resume.checked": "恢复解题",
  "provider.recovery.checked": "LLM 自恢复",
  "provider.batch_alert.checked": "批量错误告警",
  "directive.consumed": "Directive 消费",
  "lifecycle.checked": "生命周期",
};

export function startupTestCheckLabel(id?: string): string {
  if (!id) return "检查项";
  return CHECK_LABELS[id] || id;
}

export function startupTestStatusLabel({ busy, passed, total, failed }: StartupTestStatusInput): string {
  if (busy) return `运行中 · ${passed}/${total || "—"}`;
  if (failed > 0) return `${failed} 项失败`;
  if (total > 0) return `${passed}/${total} 通过`;
  return "待启动";
}


export type StartupTestEventTone = "good" | "bad" | "warn" | "info" | "muted";

export type StartupTestEventLike = StartupTestAlertLike & {
  seq?: number;
  ts?: number;
  type?: string;
  worker_id?: string;
  check_id?: string;
  phase?: string;
  event_type?: string;
  detail?: string;
  user_message?: string;
  raw_message?: string;
  suggested_action?: string;
  ok?: boolean | null;
  retryable?: boolean | null;
  summary?: { ok?: boolean | null } | null;
};

const CLEANUP_PHASES = new Set(["cleanup", "teardown", "cancelled"]);

export function startupTestEventLabel(ev: StartupTestEventLike): string {
  if (ev.type === "test.started") return "测试开始";
  if (ev.type === "provider.batch_alert") return "批量告警";
  if (ev.type === "provider.error") return "LLM 错误";
  if (ev.type === "flow.check") return ev.ok === false ? "流程失败" : ev.ok === true ? "流程通过" : "流程检查";
  if (ev.type === "test.done") {
    const ok = ev.summary?.ok ?? ev.ok;
    return ok === false ? "测试结束" : "测试完成";
  }
  if (ev.type === "worker.phase") {
    if (ev.ok === true) return "Worker 通过";
    if (ev.ok === false) return "Worker 失败";
    if (CLEANUP_PHASES.has((ev.phase || "").toLowerCase())) return "清理收尾";
    return "Worker 阶段";
  }
  if (ev.type === "worker.event") return "Worker 事件";
  return ev.event_type || ev.type || "系统事件";
}

export function startupTestEventTone(ev: StartupTestEventLike): StartupTestEventTone {
  if (ev.type === "provider.batch_alert" || ev.should_pause_dispatch || ev.severity === "fatal") return "bad";
  if (ev.ok === false || ev.type === "test.done" && (ev.summary?.ok === false)) return "bad";
  if (ev.type === "provider.error") return ev.retryable ? "warn" : "bad";
  if (ev.ok === true || ev.type === "test.done" && (ev.summary?.ok === true)) return "good";
  if (CLEANUP_PHASES.has((ev.phase || "").toLowerCase())) return "muted";
  if (ev.type === "test.started" || ev.type === "flow.check" || ev.type === "worker.phase" || ev.type === "worker.event") return "info";
  return "muted";
}

export function startupTestEventSubject(ev: StartupTestEventLike): string {
  if (ev.worker_id) return ev.worker_id;
  if (ev.check_id) return startupTestCheckLabel(ev.check_id);
  if (ev.provider) return ev.account_id ? `${ev.provider} · ${ev.account_id}` : ev.provider;
  return "系统";
}

export function startupTestEventDetail(ev: StartupTestEventLike): string {
  const primary = ev.user_message || ev.detail || ev.raw_message || ev.event_type || ev.phase || "等待事件详情";
  const normalized = ev.detail && primary === ev.detail ? startupTestWorkerDetail(ev.detail) : primary;
  return [normalized, ev.suggested_action].filter(Boolean).join(" · ");
}

export function startupTestEventTime(ev: StartupTestEventLike): string {
  if (!ev.ts) return "--:--:--";
  const millis = ev.ts < 10_000_000_000 ? ev.ts * 1000 : ev.ts;
  const date = new Date(millis);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}
