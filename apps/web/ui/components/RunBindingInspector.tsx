"use client";

import type { RunBindingRef, RunBindingSnapshot, RunObservabilitySnapshot } from "@/lib/useRun";
import { Icon, type IconName } from "@/components/Icon";
import { bindingEndpoint, bindingModel, bindingProvider, bindingRole, bindingText, workerProfileLabel } from "@/lib/runBinding";
import { useLang } from "@/lib/i18n";

type RoleKey = "reason" | "worker" | "review" | "observer" | "titler";

const ROLE_META: ReadonlyArray<{ key: RoleKey; label: string; icon: IconName }> = [
  { key: "reason", label: "Reason", icon: "network" },
  { key: "worker", label: "Worker", icon: "terminal" },
  { key: "review", label: "Reviewer", icon: "alert" },
  { key: "observer", label: "Observer", icon: "eye" },
  { key: "titler", label: "Titler", icon: "pencil" },
];
function displayBinding(ref: RunBindingRef | string | undefined, zh = false): string {
  if (typeof ref === "string") return ref || (zh ? "不可用" : "Unavailable");
  if (!ref || !Object.keys(ref).length) return zh ? "不可用" : "Unavailable";
  const provider = bindingProvider(ref);
  const model = bindingModel(ref);
  return model ? `${provider} / ${model}` : provider;
}

function formatUsd(value?: number): string {
  return `$${Number(value || 0).toFixed(3)}`;
}

function formatElapsed(ms?: number): string {
  if (!ms) return "--";
  const total = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes ? `${minutes}m ${String(seconds).padStart(2, "0")}s` : `${seconds}s`;
}

function statusLabel(status?: string, zh = false): string {
  const raw = String(status || "unknown").replaceAll("_", " ");
  if (!zh) return raw;
  return ({ healthy: "正常", unknown: "未知", ready: "就绪", active: "运行中", degraded: "已降级", blocked: "已阻断", unconfigured: "未配置" } as Record<string, string>)[raw] || raw;
}

export function RunBindingInspector({
  snapshot,
  compact = false,
  observability,
}: {
  snapshot?: RunBindingSnapshot | null;
  compact?: boolean;
  observability?: RunObservabilitySnapshot | null;
}) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const observedRoles = new Map((observability?.binding.roles || []).map((row) => [row.role, row]));
  const hasSnapshot = Boolean(snapshot);
  const hasRuntimeProjection = Boolean(observability?.binding);
  const frozen = observability?.binding.frozen ?? hasSnapshot;
  const source = observability?.binding.source || (hasSnapshot ? "frozen_snapshot" : "unavailable");
  const health = observability?.health.status || "unknown";
  const title = hasSnapshot ? (zh ? "启动快照" : "Launch snapshot") : hasRuntimeProjection ? (zh ? "运行时观测" : "Runtime observability") : (zh ? "绑定不可用" : "Binding unavailable");
  const profile = hasSnapshot ? workerProfileLabel(snapshot) : (zh ? "运行时角色覆盖" : "Runtime role overlay");
  // docs/12 §5.1: a legacy Run without a frozen snapshot shows a single
  // "legacy / not frozen" notice; its per-role provider/model state is never
  // fabricated. Roles observed at runtime with empty binding refs are legacy too.
  const rolesHaveBinding = (observability?.binding.roles || []).some(
    (row) =>
      bindingText(row.configured, "provider_ref", "provider", "label") !== "" ||
      bindingText(row.frozen, "provider_ref", "provider", "label") !== "" ||
      bindingText(row.effective, "provider_ref", "provider", "label") !== "",
  );
  const legacy = !hasSnapshot && !rolesHaveBinding;

  return (
    <section className={`run-binding-inspector ${compact ? "compact" : ""}`} aria-label={zh ? "Run 绑定与运行观测" : "Run binding and observability"}>
      <div className="run-binding-head">
        <div>
          <span className="eyebrow">{zh ? "绑定 / 运行观测" : "BINDINGS / OBSERVABILITY"}</span>
          <strong>{title}</strong>
        </div>
        <span className={`run-binding-frozen health-${health}`}>
          <Icon name={frozen ? "lock" : "alert"} size={11} />
          {frozen ? `${zh ? "已冻结" : "frozen"} v${observability?.binding.version || snapshot?.version || 1}` : (zh ? "历史运行 / 未冻结" : "legacy / not frozen")}
        </span>
      </div>

      {legacy ? (
        <p className="run-binding-legacy">
          {zh ? "该 Run 创建于冻结绑定快照启用之前。Provider、模型和端点路由没有被记录，因此不会重建；回放、证据和用量历史仍然可用。" : "This run predates frozen binding snapshots. Its provider, model, and endpoint routing were not recorded and are intentionally not reconstructed. Replay, evidence, and usage history remain available."}
        </p>
      ) : (
        <>
          <div className="run-binding-profile">
            <Icon name="cpu" size={13} />
            <span>{zh ? "Worker 配置" : "Worker Profile"}</span>
            <b title={profile}>{profile}</b>
          </div>

          <div className="run-binding-grid">
            {ROLE_META.map((role) => {
              const observed = observedRoles.get(role.key);
              const configured = observed?.configured || (hasSnapshot ? bindingRole(snapshot, role.key) : undefined);
              const frozenRef = observed?.frozen || (hasSnapshot ? bindingRole(snapshot, role.key) : undefined);
              const effective = observed?.effective || frozenRef || configured;
              const provider = bindingProvider(effective);
              const model = bindingModel(effective);
              const host = bindingEndpoint(typeof effective?.base_url === "string" ? effective.base_url : "");
              const usage = observed?.usage;
              const status = observed?.status || (effective ? "ready" : "unconfigured");
              const issues = observed?.issues || [];
              return (
                <article className="run-binding-role" key={role.key}>
                  <div className="run-binding-role-head">
                    <Icon name={role.icon} size={12} />
                    <span>{role.label}</span>
                    <em className={`run-binding-status status-${status}`}>{statusLabel(status, zh)}</em>
                  </div>
                  <strong title={`${provider}${model ? ` / ${model}` : ""}`}>{provider}</strong>
                  <small title={model || undefined}>{model || (zh ? "未记录模型" : "No model recorded")}</small>
                  {host && <small className="run-binding-host" title={host}>{host}</small>}
                  {!compact && (
                    <div className="run-binding-layers">
                      <span><i>{zh ? "已配置" : "configured"}</i><b>{displayBinding(configured, zh)}</b></span>
                      <span><i>{zh ? "已冻结" : "frozen"}</i><b>{displayBinding(frozenRef, zh)}</b></span>
                      <span><i>{zh ? "生效配置" : "effective"}</i><b>{displayBinding(effective, zh)}</b></span>
                    </div>
                  )}

                  {observed && (
                    <div className="run-binding-usage">
                      <span>{usage?.calls ?? 0} {zh ? "次调用" : "calls"}</span>
                      <span>{usage?.tokens ?? 0} {zh ? "令牌" : "tok"}</span>
                      <span>{formatUsd(usage?.usd)}</span>
                      {!compact && usage && (usage.unknown_calls > 0 || usage.estimated_calls > 0) && (
                        <span className="run-binding-usage-qualifier">
                          {usage.unknown_calls ? `${usage.unknown_calls} ${zh ? "未知" : "unknown"}` : `${usage.estimated_calls} ${zh ? "估算" : "estimated"}`}
                        </span>
                      )}
                    </div>
                  )}
                  {!compact && observed && (
                    <small className="run-binding-source">
                      {observed.configured_source || observed.effective_source || source}
                      {observed.fallback_candidate ? ` · fallback → ${displayBinding(observed.fallback_candidate, zh)}` : ""}
                      {issues.length ? ` · ${issues.length} issue${issues.length === 1 ? "" : "s"}` : ""}
                    </small>
                  )}
                  {!compact && !observed && (
                    <small className="run-binding-source">{source}</small>
                  )}
                </article>
              );
            })}
          </div>
        </>
      )}

      <div className="run-binding-foot">
        <span>{zh ? "来源" : "Source"} {source}</span>
        <span>
          {observability
            ? `${observability.usage.tokens} ${zh ? "令牌" : "tok"} · ${formatUsd(observability.usage.usd)} · ${formatElapsed(observability.lifecycle.elapsed_ms)}`
            : (zh ? "等待运行时用量" : "Runtime usage pending")}
        </span>
      </div>

      {!compact && observability && (observability.health.issues.length > 0 || observability.health.degraded.length > 0) && (
        <div className="run-binding-health" role="status">
          <div className="run-binding-health-head"><Icon name="alert" size={12} /><strong>{zh ? "运行健康 · " : "Runtime health · "}{statusLabel(health, zh)}</strong></div>
          {observability.health.issues.slice(-6).map((issue, index) => (
            <div className="run-binding-issue" key={`${issue.kind}-${issue.ts || index}`}>
              <b>{issue.role || "runtime"}</b>
              <span>{issue.message || issue.category || issue.kind}</span>
              {issue.retryable && <em>retryable</em>}
            </div>
          ))}
          {observability.health.degraded.slice(-6).map((item, index) => (
            <div className="run-binding-issue" key={`degraded-${item.ts || index}`}>
              <b>{item.role || item.engine || "runtime"}</b>
              <span>{item.reason || "degraded"}</span>
            </div>
          ))}
        </div>
      )}

      {!compact && observability?.health.reason && observability.health.reason.state !== "unknown" && (
        <div className={`run-binding-health reason-health-${observability.health.reason.state}`} role="status">
          <div className="run-binding-health-head">
            <Icon name="network" size={12} />
            <strong>
              {zh ? "Reason 健康 · " : "Reason health · "}
              {statusLabel(observability.health.reason.state, zh)}
            </strong>
          </div>
          <div className="run-binding-source">
            {zh
              ? `失败 ${observability.health.reason.failures}/${observability.health.reason.max_failures} · 请求 ${observability.health.reason.request_failures}/${observability.health.reason.request_limit} · 窗口 ${observability.health.reason.retry_window_s}s`
              : `failures ${observability.health.reason.failures}/${observability.health.reason.max_failures} · requests ${observability.health.reason.request_failures}/${observability.health.reason.request_limit} · window ${observability.health.reason.retry_window_s}s`}
            {observability.health.reason.retry_not_before
              ? ` · ${zh ? "下次重试 " : "next retry "}${formatElapsed((observability.health.reason.retry_not_before - observability.generated_at) * 1000)}`
              : ""}
            {observability.health.reason.immediate ? ` · ${zh ? "立即熔断" : "immediate circuit"}` : ""}
            {observability.health.reason.fallback_active ? ` · ${zh ? "已切换设置默认" : "settings default active"}` : ""}
          </div>
          {observability.health.reason.blocked_reason && (
            <div className="run-binding-issue">
              <b>reason</b>
              <span>{observability.health.reason.blocked_reason}</span>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
