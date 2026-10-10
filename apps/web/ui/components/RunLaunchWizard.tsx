"use client";

import { useEffect, useMemo, useState } from "react";
import { Icon } from "@/components/Icon";
import { getWorkerSettingsWorkspace, type LLMProvider, type ReasonLlmProfile, type WorkerSettingsWorkspace } from "@/lib/useRun";
import { createTargetDirection, type TargetRecord } from "@/lib/useTargets";
import { normalizeTargetHints, type TargetHintForLaunch } from "@/lib/targetHints";

export interface LaunchBindingSelection {
  directionId?: string;
  direction?: string;
  prompt: string;
  workerProfileId: string;
  llmMode: "settings_default" | "per_run_override";
  llmOverrides: {
    reason?: Record<string, unknown>;
    worker?: Record<string, unknown>;
    review?: Record<string, unknown>;
  };
  review: Record<string, unknown>;
  inheritVerifiedFacts: boolean;
  verifiedFactIds: string[];
  targetHints: TargetHintForLaunch[];
  includeTargetHints: boolean;
  targetHintIds: string[];
}

type Step = 0 | 1 | 2;

function providerProfile(provider: LLMProvider | undefined, base: ReasonLlmProfile | undefined, model: string): Record<string, unknown> {
  const providerRef = provider?.id || base?.provider_ref || "";
  return {
    provider: provider?.kind || provider?.id || base?.provider || "openai",
    provider_ref: providerRef,
    model: model || provider?.default_model || base?.model || "",
    base_url: provider?.base_url || base?.base_url || "",
    wire_api: provider?.wire_api || base?.wire_api || "auto",
    credential_source: providerRef ? "provider" : (base?.credential_source || "auto"),
    credential_account: providerRef ? "" : (base?.credential_account || "pi-main"),
    effort: base?.effort,
    timeout: base?.timeout,
  };
}

function providerFor(workspace: WorkerSettingsWorkspace | null, ref: string): LLMProvider | undefined {
  return workspace?.config.llm_providers?.find((provider) => provider.id === ref) || workspace?.config.llm_providers?.[0];
}
export function RunLaunchWizard({ target, onClose, onLaunch }: { target: TargetRecord; onClose: () => void; onLaunch: (selection: LaunchBindingSelection) => void | Promise<void> }) {
  const [step, setStep] = useState<Step>(0);
  const [workspace, setWorkspace] = useState<WorkerSettingsWorkspace | null>(null);
  const [loading, setLoading] = useState(true);
  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState("");
  const [prompt, setPrompt] = useState(target.description || "");
  const [directionId, setDirectionId] = useState(String(target.primary_direction_id || ""));
  const [workerProfileId, setWorkerProfileId] = useState("");
  const [reasonProviderId, setReasonProviderId] = useState("");
  const [reasonModel, setReasonModel] = useState("");
  const [workerProviderId, setWorkerProviderId] = useState("");
  const [workerModel, setWorkerModel] = useState("");
  const [reviewProviderId, setReviewProviderId] = useState("");
  const [reviewModel, setReviewModel] = useState("");
  const [overrideLlm, setOverrideLlm] = useState(false);
  const [reviewEnabled, setReviewEnabled] = useState(true);
  const [knowledgeMode, setKnowledgeMode] = useState<"none" | "all" | "selected">("none");
  const [selectedFactIds, setSelectedFactIds] = useState<string[]>([]);
  const [includeTargetHints, setIncludeTargetHints] = useState(true);
  const [directions, setDirections] = useState<Array<Record<string, unknown>>>(() => Array.isArray(target.directions) ? target.directions : []);
  const [newDirectionOpen, setNewDirectionOpen] = useState(false);
  const [newDirectionLabel, setNewDirectionLabel] = useState("");
  const [newDirectionDescription, setNewDirectionDescription] = useState("");
  const [directionSaving, setDirectionSaving] = useState(false);
  const [directionError, setDirectionError] = useState("");

  useEffect(() => {
    let alive = true;
    getWorkerSettingsWorkspace().then((next) => {
      if (!alive) return;
      setWorkspace(next);
      const config = next?.config;
      const llmProfiles = (config?.llm_profiles || {}) as Record<string, ReasonLlmProfile>;
      const planner = llmProfiles.reason || llmProfiles.planner;
      const worker = llmProfiles.worker;
      const review = llmProfiles.review;
      const firstProvider = config?.llm_providers?.[0];
      setWorkerProfileId(config?.worker_profiles?.find((profile) => profile.enabled)?.name || config?.worker_profiles?.find((profile) => profile.enabled)?.id || config?.engines?.[0] || "pi");
      setReasonProviderId(planner?.provider_ref || firstProvider?.id || "");
      setReasonModel(planner?.model || firstProvider?.default_model || "");
      setWorkerProviderId(worker?.provider_ref || firstProvider?.id || "");
      setWorkerModel(worker?.model || firstProvider?.default_model || "");
      setReviewProviderId(review?.provider_ref || planner?.provider_ref || firstProvider?.id || "");
      setReviewModel(review?.model || planner?.model || firstProvider?.default_model || "");
      setReviewEnabled(config?.review_policy?.enabled !== false);
      setLoading(false);
    }).catch(() => setLoading(false));
    return () => { alive = false; };
  }, [target.id]);

  useEffect(() => {
    const next = Array.isArray(target.directions) ? target.directions : [];
    setDirections(next);
    setDirectionId(String(target.primary_direction_id || ""));
    setNewDirectionOpen(false);
    setNewDirectionLabel("");
    setNewDirectionDescription("");
    setDirectionError("");
  }, [target.id, target.directions, target.primary_direction_id]);
  const profiles = useMemo(() => (workspace?.config.worker_profiles || []).filter((profile) => profile.enabled), [workspace]);
  const providers = workspace?.config.llm_providers || [];
  const reasonProvider = providerFor(workspace, reasonProviderId);
  const selectedDirection = directions.find((item) => String(item.id || "") === directionId);
  const targetHints = useMemo(() => normalizeTargetHints(target.hints), [target.hints]);
  const currentProfile = profiles.find((profile) => (profile.name || profile.id) === workerProfileId);

  const addDirection = async () => {
    const label = newDirectionLabel.trim();
    if (!label || directionSaving) return;
    setDirectionSaving(true);
    setDirectionError("");
    const created = await createTargetDirection(target.id, {
      label,
      description: newDirectionDescription.trim(),
      category: target.category || "ctf",
    });
    setDirectionSaving(false);
    if (!created) {
      setDirectionError("Could not save direction.");
      return;
    }
    setDirections((current) => [...current, created]);
    setDirectionId(String(created.id || ""));
    setNewDirectionOpen(false);
    setNewDirectionLabel("");
    setNewDirectionDescription("");
  };
  const launch = async () => {
    const nextPrompt = prompt.trim();
    if (!nextPrompt) return;
    setLaunching(true);
    setError("");
    try {
      const config = workspace?.config;
      const profiles = (config?.llm_profiles || {}) as Record<string, ReasonLlmProfile>;
      const llmOverrides = overrideLlm ? {
        reason: providerProfile(reasonProvider, profiles.reason || profiles.planner, reasonModel),
        worker: providerProfile(providerFor(workspace, workerProviderId), profiles.worker, workerModel),
        review: providerProfile(providerFor(workspace, reviewProviderId), profiles.review, reviewModel),
      } : {};
      await onLaunch({
        directionId: directionId || undefined,
        direction: String(selectedDirection?.label || "") || undefined,
        prompt: nextPrompt,
        workerProfileId: workerProfileId || "pi",
        llmMode: overrideLlm ? "per_run_override" : "settings_default",
        llmOverrides,
        review: { ...(config?.review_policy || {}), enabled: reviewEnabled },
        inheritVerifiedFacts: knowledgeMode !== "none",
        verifiedFactIds: knowledgeMode === "all" ? [] : selectedFactIds,
        targetHints: includeTargetHints ? targetHints : [],
        includeTargetHints,
        targetHintIds: includeTargetHints ? targetHints.map((hint) => hint.id) : [],
      });
    } catch (err) {
      const detail = err instanceof Error ? err.message.trim() : "";
      setError(detail || "启动失败，请检查后端状态后重试。");
    } finally { setLaunching(false); }
  };

  return (
    <div className="launch-wizard-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !launching) onClose(); }}>
      <section className="launch-wizard" role="dialog" aria-modal="true" aria-labelledby="launch-wizard-title">
        <header className="launch-wizard-head">
          <div><span className="eyebrow">RUN / NEW</span><h2 id="launch-wizard-title">新建 Run</h2><p>{target.name} · 每个 Run 都是一次独立的智能体集群。</p></div>
          <button className="target-icon-button" type="button" onClick={onClose} disabled={launching} aria-label="关闭"><Icon name="x" size={17} /></button>
        </header>
        <nav className="launch-wizard-steps" aria-label="Run 创建步骤">
          {["方向与提示词", "Worker 配置", "高级绑定"].map((label, index) => <button type="button" key={label} className={step === index ? "active" : step > index ? "done" : ""} onClick={() => setStep(index as Step)}><b>{index + 1}</b><span>{label}</span></button>)}
        </nav>
        <div className="launch-wizard-body">
          {step === 0 && (
            <div className="launch-wizard-pane">
              <div className="launch-target-preview"><Icon name="target" size={20} /><div><strong>{target.name}</strong><span>{target.category || "misc"} · {target.target_address || "未记录地址"}</span></div><em>{target.run_ids?.length || 0} 个历史 Run</em></div>
              <label className="wizard-field wizard-prompt-field"><span>本次 Run 提示词 *</span><textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={6} placeholder="描述这次 Run 希望 Reason 和 Worker 优先验证什么。默认读取 Target 描述。" /><small>Target 的题目描述、地址、Flag 格式和保存的提示会继续作为默认上下文注入。</small></label>
              <div className="wizard-field wizard-direction-field"><div className="wizard-field-head"><span>主方向</span><button type="button" className="wizard-inline-action" onClick={() => { setNewDirectionOpen((current) => !current); setDirectionError(""); }}>{newDirectionOpen ? "取消" : "+ 新建方向"}</button></div><select value={directionId} onChange={(event) => setDirectionId(event.target.value)}><option value="">综合探索</option>{directions.map((direction) => <option key={String(direction.id)} value={String(direction.id)}>{String(direction.label || direction.name || "未命名方向")}</option>)}</select>{newDirectionOpen && <div className="wizard-new-direction"><input value={newDirectionLabel} onChange={(event) => setNewDirectionLabel(event.target.value)} placeholder="方向名称" /><input value={newDirectionDescription} onChange={(event) => setNewDirectionDescription(event.target.value)} placeholder="方向说明" /><button type="button" className="target-ghost-button" onClick={() => void addDirection()} disabled={directionSaving}>{directionSaving ? "保存中…" : "保存方向"}</button>{directionError && <span className="wizard-inline-error">{directionError}</span>}</div>}<small>主方向决定初始关注点；Reason 可以在运行中调度其他方向的 Worker。</small></div>
              <label className="wizard-toggle"><input type="checkbox" checked={includeTargetHints} onChange={(event) => setIncludeTargetHints(event.target.checked)} /><span><b>读取 Target 提示</b><small>作为指导上下文读取，不会绕过执行证据门禁。</small></span></label>
            </div>
          )}
          {step === 1 && <div className="launch-wizard-pane"><label className="wizard-field"><span>Worker Profile</span><select value={workerProfileId} onChange={(event) => setWorkerProfileId(event.target.value)}>{profiles.length ? profiles.map((profile) => <option key={profile.id} value={profile.name || profile.id}>{profile.label || profile.name || profile.id} · {profile.engine}</option>) : <option value="pi">pi (default)</option>}</select><small>{currentProfile ? `${currentProfile.transport} · ${currentProfile.runtime}` : "使用已配置的 pi Worker。"}</small></label><div className="wizard-binding-card"><span className="eyebrow">TARGET CONTEXT</span><strong>{target.name}</strong><p>{target.description || "没有额外描述。"}</p><p>本次提示词和 Target 信息会一起交给 Reason。</p></div></div>}
          {step === 2 && <div className="launch-wizard-pane"><label className="wizard-toggle"><input type="checkbox" checked={overrideLlm} onChange={(event) => setOverrideLlm(event.target.checked)} /><span><b>{overrideLlm ? "本 Run 覆盖 LLM" : "使用设置默认（推荐）"}</b><small>{overrideLlm ? "仅覆盖 Reason、Worker、Reviewer；Observer 与 Titler 始终使用设置默认。" : "新 Run 直接继承设置中的 Reason、Worker、Reviewer、Observer、Titler 默认配置。"}</small></span></label>{overrideLlm && <div className="wizard-role-grid"><RoleBinding label="Reason" value={reasonProviderId} model={reasonModel} providers={providers} onProvider={setReasonProviderId} onModel={setReasonModel} /><RoleBinding label="Worker" value={workerProviderId} model={workerModel} providers={providers} onProvider={setWorkerProviderId} onModel={setWorkerModel} /><RoleBinding label="Reviewer" value={reviewProviderId} model={reviewModel} providers={providers} onProvider={setReviewProviderId} onModel={setReviewModel} /></div>}<label className="wizard-toggle"><input type="checkbox" checked={reviewEnabled} onChange={(event) => setReviewEnabled(event.target.checked)} /><span><b>启用 Reviewer 质量闸门</b><small>Reviewer 不可用时，普通业务 Intent 会阻断；不会自动信任 Worker。</small></span></label><div className="wizard-binding-summary"><span>设置默认</span><b>Reason {reasonModel || "未配置"} · Worker {workerModel || "未配置"} · Reviewer {reviewModel || "未配置"}</b><small>Observer / Titler 使用设置默认，不支持 Run 覆盖。</small></div></div>}
        </div>
        <footer className="launch-wizard-foot">
          {error && <p className="launch-wizard-error" role="alert">{error}</p>}
          <span className="launch-wizard-spacer" />
          {step < 2 ? <button className="target-primary-button" type="button" onClick={() => setStep((step + 1) as Step)} disabled={loading || (step === 0 && !prompt.trim())}>{loading ? "读取配置…" : "继续"}<Icon name="chevronRight" size={13} /></button> : <button className="target-primary-button" type="button" onClick={() => void launch()} disabled={launching || loading || !prompt.trim()}>{launching ? "启动中…" : "创建并启动 Run"}<Icon name="play" size={13} /></button>}
        </footer>
      </section>
    </div>
  );
}

function RoleBinding({ label, value, model, providers, onProvider, onModel }: { label: string; value: string; model: string; providers: LLMProvider[]; onProvider: (value: string) => void; onModel: (value: string) => void }) {
  const provider = providers.find((item) => item.id === value) || providers[0];
  const models = provider?.models || [];
  return <div className="wizard-role-card"><div className="wizard-role-head"><Icon name={label === "Reason" ? "network" : label === "Worker" ? "terminal" : label === "Review" ? "alert" : "pencil"} size={15} /><strong>{label}</strong></div><label><span>Provider</span><select value={value || provider?.id || ""} onChange={(event) => { onProvider(event.target.value); const next = providers.find((item) => item.id === event.target.value); if (next?.default_model) onModel(next.default_model); }}>{providers.length ? providers.map((item) => <option key={item.id} value={item.id}>{item.label || item.id}</option>) : <option value="">Configured default</option>}</select></label><label><span>Model</span>{models.length ? <select value={model} onChange={(event) => onModel(event.target.value)}>{models.map((item) => <option key={item} value={item}>{item}</option>)}</select> : <input value={model} onChange={(event) => onModel(event.target.value)} placeholder={provider?.default_model || "model id"} />}</label></div>;
}



