"use client";

import {
  useCallback, useEffect, useMemo, useRef, useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  applyWorkerSettingsDraft, getWorkerSettingsWorkspace, probeLlmProvider, probeWorkerEndpoint, testLlmEndpoint,
  validateWorkerSettingsDraft, type CredentialAccount, type WorkerSecretUpdate,
  type LLMProvider, type LLMProviderSecretMeta, type LLMProviderTemplate, type ProviderSecretUpdate,
  type ReasonLlmProbe, type WorkerEndpointProbe, type WorkerSettings, type WorkerSettingsValidation,
  type WorkerSettingsWorkspace as Workspace,
} from "@/lib/useRun";
import { useLang } from "@/lib/i18n";
import { readKey, writeKey } from "@/lib/storage";
import {
  BUILTIN_RUNTIME_IDS, WORKER_DIRECTIONS, cloneRuntimeForDirection,
  configuredAccount, copyWorkerFields, customWorkers, directionForProfile,
  endpointForProfile, profileLabel, setSecretUpdate, synthesizeDirectionProfiles, batchWorkerFields,
  systemWorker, workerReadiness, type DirectionKey, type WorkerProfile,
} from "@/lib/workerSettingsDraft";
import {
  workerSettingsIssueMessage, workerSettingsText, type WorkerSettingsVars,
} from "@/lib/workerSettingsText";
import {
  WORKER_SETTINGS_MASTER_DEFAULT, WORKER_SETTINGS_MASTER_MIN,
  WORKER_SETTINGS_DETAIL_MIN, WORKER_SETTINGS_SPLITTER_WIDTH,
  WORKER_SETTINGS_SPLIT_STORAGE_KEY, clampWorkerSettingsMasterWidth,
  defaultWorkerSettingsMasterWidth,
} from "@/lib/workerSettingsSizing";

type Section = "workers" | "reason" | "providers";
type AuxSection = "runtimes" | "diagnostics";
type AuxNavItem = [AuxSection, string, string];
type ThemeMode = "light" | "dark";
type LlmProfileKey = "planner" | "titler";

const DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1";
type TestState = { busy: boolean; ok?: boolean; detail?: string; models?: { id: string; name: string }[]; detectedWireApi?: string | null; probe?: WorkerEndpointProbe };
type Notice = { key?: string; detail?: string; tone: "good" | "bad" };
type WswText = (key: string, vars?: WorkerSettingsVars) => string;

const clone = <T,>(value: T): T => structuredClone(value);
const stable = (value: unknown) => JSON.stringify(value);
const directionProfile = (profiles: WorkerProfile[], key: DirectionKey) =>
  profiles.find((profile) => directionForProfile(profile) === key);

function useWswText(): { lang: "zh" | "en"; w: WswText } {
  const { lang } = useLang();
  return {
    lang,
    w: useCallback(
      (key: string, vars?: WorkerSettingsVars) => workerSettingsText(lang, key, vars),
      [lang],
    ),
  };
}

function host(value: string, fallback: string): string {
  try { return new URL(value).host; } catch { return value || fallback; }
}

function translatedMissing(w: WswText, missing: string[]): string {
  return missing.map((item) => w(`missing.${item}`)).join(", ");
}

export function WorkerSettingsWorkspace() {
  const { lang, setLang } = useLang();
  const { w } = useWswText();
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [draft, setDraft] = useState<WorkerSettings | null>(null);
  const [accounts, setAccounts] = useState<CredentialAccount[]>([]);
  const [secrets, setSecrets] = useState<WorkerSecretUpdate[]>([]);
  const [providerSecrets, setProviderSecrets] = useState<ProviderSecretUpdate[]>([]);
  const [section, setSection] = useState<Section | null>("workers");
  const [auxSection, setAuxSection] = useState<AuxSection | null>(null);
  const [selected, setSelected] = useState("pi-web");
  const [compactEditor, setCompactEditor] = useState(false);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [validation, setValidation] = useState<WorkerSettingsValidation | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [applying, setApplying] = useState(false);
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [showSystem, setShowSystem] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchSelected, setBatchSelected] = useState<string[]>([]);
  const [batchProvider, setBatchProvider] = useState("");
  const [batchModel, setBatchModel] = useState("");
  const [theme, setTheme] = useState<ThemeMode>("dark");
  const [themeReady, setThemeReady] = useState(false);
  const [masterWidth, setMasterWidth] = useState(WORKER_SETTINGS_MASTER_DEFAULT);
  const [splitReady, setSplitReady] = useState(false);
  const [resizing, setResizing] = useState(false);
  const splitRef = useRef<HTMLDivElement | null>(null);
  const pointerCleanupRef = useRef<(() => void) | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setNotice(null);
    const value = await getWorkerSettingsWorkspace();
    if (!value) {
      setNotice({ key: "loadError", tone: "bad" }); setLoading(false); return;
    }
    const config = synthesizeDirectionProfiles({ ...value.config, llm_providers: value.config.llm_providers || [] });
    setWorkspace({ ...value, config }); setDraft(clone(config));
    setAccounts(value.accounts); setSecrets([]); setProviderSecrets([]); setValidation(null); setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const saved = readKey("dswarm.theme");
    const next: ThemeMode = saved === "light" || saved === "dark" ? saved : "dark";
    setTheme(next); document.documentElement.dataset.theme = next; setThemeReady(true);
  }, []);
  useEffect(() => {
    if (!themeReady) return;
    document.documentElement.dataset.theme = theme; writeKey("dswarm.theme", theme);
  }, [theme, themeReady]);
  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  const dirty = useMemo(
    () => Boolean(workspace && draft) &&
      (stable(workspace?.config) !== stable(draft) || secrets.length > 0 || providerSecrets.length > 0),
    [workspace, draft, secrets, providerSecrets],
  );
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (!dirty) return; event.preventDefault(); event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    if (!draft || section !== "workers" || splitReady || !splitRef.current) return;
    const containerWidth = splitRef.current.getBoundingClientRect().width;
    const saved = Number(readKey(WORKER_SETTINGS_SPLIT_STORAGE_KEY));
    setMasterWidth(Number.isFinite(saved) && saved > 0
      ? clampWorkerSettingsMasterWidth(saved, containerWidth)
      : defaultWorkerSettingsMasterWidth(containerWidth));
    setSplitReady(true);
  }, [draft, section, splitReady]);
  useEffect(() => {
    if (splitReady) writeKey(WORKER_SETTINGS_SPLIT_STORAGE_KEY, String(masterWidth));
  }, [masterWidth, splitReady]);
  useEffect(() => {
    const clampToContainer = () => {
      const width = splitRef.current?.getBoundingClientRect().width;
      if (width) setMasterWidth((current) => clampWorkerSettingsMasterWidth(current, width));
    };
    window.addEventListener("resize", clampToContainer);
    return () => window.removeEventListener("resize", clampToContainer);
  }, []);
  useEffect(() => () => {
    pointerCleanupRef.current?.(); document.body.classList.remove("wsw-resizing");
  }, []);

  const selectedProfile = draft?.worker_profiles.find(
    (profile) => profileLabel(profile) === selected || profile.id === selected,
  );
  const patchProfile = (label: string, patch: Partial<WorkerProfile>) => {
    setDraft((current) => current ? {
      ...current,
      worker_profiles: current.worker_profiles.map((profile) =>
        profileLabel(profile) === label ? { ...profile, ...patch } : profile),
    } : current);
    setValidation(null); setNotice(null);
  };
  const patchRuntime = (id: string, patch: Partial<WorkerSettings["runtime_profiles"][number]>) => {
    setDraft((current) => current ? {
      ...current,
      runtime_profiles: current.runtime_profiles.map((runtime) =>
        runtime.id === id ? { ...runtime, ...patch } : runtime),
    } : current);
    setValidation(null);
  };
  const choose = (label: string) => {
    setSelected(label); setCompactEditor(true); setSection("workers"); setAuxSection(null);
  };
  const validate = async () => {
    if (!draft) return null;
    const value = await validateWorkerSettingsDraft(draft, secrets, providerSecrets);
    setValidation(value); return value;
  };
  const requestApply = async () => {
    const value = await validate(); if (value) setReviewOpen(true);
  };
  const confirmApply = async () => {
    if (!workspace || !draft || !validation?.ok) return;
    setApplying(true); setNotice(null);
    const result = await applyWorkerSettingsDraft(workspace.revision, draft, secrets, providerSecrets);
    setApplying(false);
    if (!result.ok || !result.workspace) {
      setNotice({
        key: result.conflict ? "conflict" : result.detail ? undefined : "applyFailed",
        detail: result.conflict ? undefined : result.detail, tone: "bad",
      });
      return;
    }
    const config = synthesizeDirectionProfiles(result.workspace.config);
    setWorkspace({ ...result.workspace, config }); setDraft(clone(config));
    setAccounts(result.workspace.accounts); setSecrets([]); setProviderSecrets([]); setValidation(null);
    setReviewOpen(false); setNotice({ key: "applied", tone: "good" });
  };
  const revert = () => {
    if (!workspace) return;
    setDraft(clone(workspace.config)); setSecrets([]); setProviderSecrets([]); setValidation(null);
    setNotice({ key: "reverted", tone: "good" });
  };
  const applyBatch = () => {
    if (!draft || !batchSelected.length || !batchProvider) return;
    const provider = (draft.llm_providers || []).find((item) => item.id === batchProvider);
    const model = batchModel || provider?.default_model || provider?.models?.[0] || "";
    setDraft((current) => current ? {
      ...current,
      worker_profiles: batchWorkerFields(current.worker_profiles, batchSelected, {
        provider_ref: batchProvider,
        model,
      }),
    } : current);
    setValidation(null); setNotice(null); setBatchOpen(false);
  };

  const testProfile = async (profile: WorkerProfile, apiKey = "", validateModel = false) => {
    const label = profileLabel(profile);
    setTests((current) => ({ ...current, [label]: { busy: true } }));
    const result = await probeWorkerEndpoint(profile, apiKey, validateModel);
    if (result.detected_wire_api && profile.wire_api === "auto") {
      patchProfile(label, { wire_api: result.detected_wire_api });
    }
    setTests((current) => ({ ...current, [label]: {
      busy: false, ok: Boolean(result.ok), detail: result.detail || w("connectionFailed"),
      models: result.models, detectedWireApi: result.detected_wire_api, probe: result,
    } }));
  };
  const addCustom = () => {
    if (!draft) return;
    const base = systemWorker(draft.worker_profiles) || draft.worker_profiles[0];
    if (!base) return;
    const id = `pi-custom-${Math.random().toString(36).slice(2, 7)}`;
    const profile = { ...clone(base), id, name: id, label: id,
      credential_account: `${id}-main`, enabled: false,
      roles: ["respond", "review"] } as WorkerProfile;
    setDraft({ ...draft, worker_profiles: [...draft.worker_profiles, profile] });
    setSelected(id); setShowAdvanced(true); setCompactEditor(true);
  };

  const stageProviderSecret = (providerId: string, action: "retain" | "replace" | "remove", value = "") => {
    const pid = providerId.trim();
    if (!pid) return;
    setProviderSecrets((current) => {
      const rest = current.filter((row) => row.provider_id !== pid);
      if (action === "retain") return rest;
      if (action === "remove") return [...rest, { provider_id: pid, action: "remove" }];
      const secret = value.trim();
      return secret ? [...rest, { provider_id: pid, action: "replace", value: secret }] : rest;
    });
    setValidation(null); setNotice(null);
  };

  const resizeToClientX = useCallback((clientX: number, left: number, width: number) => {
    setMasterWidth(clampWorkerSettingsMasterWidth(clientX - left, width));
  }, []);
  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || !splitRef.current) return;
    event.preventDefault(); pointerCleanupRef.current?.();
    const rect = splitRef.current.getBoundingClientRect();
    const onMove = (moveEvent: PointerEvent) =>
      resizeToClientX(moveEvent.clientX, rect.left, rect.width);
    const cleanup = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", cleanup);
      window.removeEventListener("pointercancel", cleanup);
      document.body.classList.remove("wsw-resizing"); setResizing(false);
      pointerCleanupRef.current = null;
    };
    pointerCleanupRef.current = cleanup;
    document.body.classList.add("wsw-resizing"); setResizing(true);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", cleanup);
    window.addEventListener("pointercancel", cleanup);
  };
  const resizeByKeyboard = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const containerWidth = splitRef.current?.getBoundingClientRect().width;
    if (!containerWidth) return;
    let next: number | null = null;
    if (event.key === "ArrowLeft") next = masterWidth - 24;
    if (event.key === "ArrowRight") next = masterWidth + 24;
    if (event.key === "Home") next = defaultWorkerSettingsMasterWidth(containerWidth);
    if (event.key === "End") next = containerWidth - WORKER_SETTINGS_DETAIL_MIN - WORKER_SETTINGS_SPLITTER_WIDTH;
    if (next == null) return;
    event.preventDefault(); setMasterWidth(clampWorkerSettingsMasterWidth(next, containerWidth));
  };
  const resetSplit = () => {
    const width = splitRef.current?.getBoundingClientRect().width;
    if (width) setMasterWidth(defaultWorkerSettingsMasterWidth(width));
  };

  if (loading) return <div className="wsw-state"><span className="wsw-spinner" />{w("loading")}</div>;
  if (!draft || !workspace) return <div className="wsw-state wsw-error">
    {notice?.detail || w(notice?.key || "loadError")}
    <button onClick={() => void load()}>{w("retry")}</button>
  </div>;

  const rows = WORKER_DIRECTIONS.map((direction) => ({
    direction, profile: directionProfile(draft.worker_profiles, direction.key),
  })).filter((value): value is {
    direction: (typeof WORKER_DIRECTIONS)[number]; profile: WorkerProfile;
  } => Boolean(value.profile));
  const system = systemWorker(draft.worker_profiles);
  const customs = customWorkers(draft.worker_profiles);
  const errors = validation?.issues.filter((issue) => issue.severity === "error") || [];
  const warnings = validation?.issues.filter((issue) => issue.severity === "warning") || [];
  const containerWidth = splitRef.current?.getBoundingClientRect().width ||
    WORKER_SETTINGS_MASTER_DEFAULT + WORKER_SETTINGS_DETAIL_MIN + WORKER_SETTINGS_SPLITTER_WIDTH;
  const splitMax = Math.max(WORKER_SETTINGS_MASTER_MIN,
    Math.floor(containerWidth - WORKER_SETTINGS_DETAIL_MIN - WORKER_SETTINGS_SPLITTER_WIDTH));
  const navItems: Array<[Section, string, string]> = [
    ["workers", w("navWorkers"), w("navWorkersNote")],
    ["reason", w("navReason"), w("navReasonNote")],
    ["providers", w("navProviders"), w("navProvidersNote")],
  ];
  const auxItems: AuxNavItem[] = [
    ["runtimes", w("navRuntimes"), w("navRuntimesNote")],
    ["diagnostics", w("navDiagnostics"), w("navDiagnosticsNote")],
  ];

  return <div className="wsw-shell">
    <header className="wsw-topbar">
      <div className="wsw-brand">
        <a href="/" className="wsw-back" aria-label={w("allDirections")}>←</a>
        <div><span className="wsw-kicker">{w("brandKicker")}</span><h1>{w("title")}</h1></div>
      </div>
      <div className="wsw-top-actions">
        <button type="button" className="wsw-icon-btn"
          title={theme === "dark" ? w("themeLight") : w("themeDark")}
          aria-label={theme === "dark" ? w("themeLight") : w("themeDark")}
          onClick={() => setTheme((current) => current === "dark" ? "light" : "dark")}>
          {theme === "dark" ? "☀" : "☾"}
        </button>
        <button type="button" className="wsw-icon-btn wsw-lang-btn"
          title={w("langTitle")} aria-label={w("langTitle")}
          onClick={() => setLang(lang === "zh" ? "en" : "zh")}>{w("langToggle")}</button>
        <span className={`wsw-draft-state ${dirty ? "dirty" : ""}`}>
          <i />{dirty ? w("draftModified") : w("activeConfig")}
        </span>
        <button className="wsw-btn quiet" disabled={!dirty || applying} onClick={revert}>{w("revert")}</button>
        <button className="wsw-btn primary" disabled={!dirty || applying}
          onClick={() => void requestApply()}>{w("reviewApply")}</button>
      </div>
    </header>

    <div className="wsw-body">
      <nav className="wsw-nav">
        <div className="wsw-nav-label">{w("configuration")}</div>
        {navItems.map(([id, label, note]) => <button key={id}
          className={section === id ? "active" : ""}
          onClick={() => { setSection(id); setAuxSection(null); setCompactEditor(false); }}>
          <span>{label}</span><small>{note}</small>
        </button>)}
        <div className="wsw-nav-advanced">
          <div className="wsw-nav-label">{w("navAdvanced")}</div>
          <div className="wsw-nav-secondary">
            {auxItems.map(([id, label, note]) => <button key={id} type="button"
              className={auxSection === id ? "active" : ""}
              onClick={() => { setAuxSection(id); setSection(null); setCompactEditor(false); }}>
              <span>{label}</span><small>{note}</small>
            </button>)}
          </div>
        </div>
        <div className="wsw-nav-note"><b>{w("snapshotRule")}</b><span>{w("snapshotRuleNote")}</span></div>
      </nav>

      <main className="wsw-content">
        {notice && <div className={`wsw-notice ${notice.tone === "bad" ? "bad" : ""}`}>
          {notice.detail || w(notice.key || "applyFailed")}
        </div>}

        {section === "workers" && <div ref={splitRef}
          className={`wsw-workers layout-balanced ${compactEditor ? "compact-editor" : ""} ${resizing ? "resizing" : ""}`}
          style={{ "--wsw-master-width": `${masterWidth}px` } as CSSProperties}>
          <section className="wsw-master">
            <div className="wsw-heading">
              <div><span className="wsw-kicker">{w("automaticRouting")}</span>
                <h2>{w("directionWorkers")}</h2><p>{w("directionWorkersNote")}</p></div>
              <span className="wsw-count">{w("enabledCount", {
                count: rows.filter((row) => row.profile.enabled).length,
              })}</span>
            </div>
            <div className="wsw-batch-toolbar">
              <button className="wsw-btn quiet" type="button" onClick={() => setBatchOpen((value) => !value)}>{w("batchConfigure")}</button>
              {batchSelected.length > 0 && <span>{w("selectedCount", { count: batchSelected.length })}</span>}
            </div>
            {batchOpen && <div className="wsw-batch-panel">
              <div><b>{w("batchConfigure")}</b><small>{w("batchConfigureNote")}</small></div>
              <div className="wsw-batch-selectors">
                <select value={batchProvider} onChange={(event) => { setBatchProvider(event.target.value); setBatchModel(""); }}>
                  <option value="">{w("chooseProvider")}</option>
                  {(draft.llm_providers || []).map((item) => <option key={item.id} value={item.id}>{item.label || item.id}</option>)}
                </select>
                <input list="wsw-batch-models" value={batchModel} placeholder={w("modelId")} onChange={(event) => setBatchModel(event.target.value)} />
                <datalist id="wsw-batch-models">{((draft.llm_providers || []).find((item) => item.id === batchProvider)?.models || []).map((model) => <option key={model} value={model} />)}</datalist>
                <button className="wsw-btn primary" type="button" disabled={!batchSelected.length || !batchProvider} onClick={applyBatch}>{w("applyBatch")}</button>
              </div>
            </div>}
            <div className="wsw-table-head">
              <span>{w("columnDirection")}</span><span>{w("columnEndpoint")}</span>
              <span>{w("columnRuntime")}</span><span>{w("columnCapacity")}</span>
              <span>{w("columnStatus")}</span>
            </div>
            <div className="wsw-direction-list">{rows.map(({ direction, profile }) => {
              const ready = workerReadiness(profile, draft.runtime_profiles, accounts, secrets, workspace.provider_secrets || [], providerSecrets, draft.llm_providers || []);
              const boundProvider = (draft.llm_providers || []).find((provider) => provider.id === profile.provider_ref);
              const endpoint = boundProvider?.base_url || endpointForProfile(profile, accounts);
              const id = profileLabel(profile);
              return <div key={direction.key} className="wsw-direction-row-wrap">
                <input className="wsw-row-check" type="checkbox" checked={batchSelected.includes(id)}
                  aria-label={w("selectWorkerForBatch", { name: w(`direction.${direction.key}`) })}
                  onChange={(event) => setBatchSelected((current) => event.target.checked
                    ? [...current, id] : current.filter((value) => value !== id))} />
                <button className={`wsw-direction-row ${selected === id ? "selected" : ""}`}
                  onClick={() => choose(id)}>
                <span className="wsw-dir-name"><b>{w(`direction.${direction.key}`)}</b><small>{direction.id}</small></span>
                <span className="wsw-dir-stack"><b>{host(endpoint, w("notConfigured"))}</b><small>{profile.model || w("noModel")}</small></span>
                <span className="wsw-dir-stack"><b>{draft.runtime_profiles.find((runtime) => runtime.id === profile.runtime)?.label || profile.runtime}</b><small>{profile.image || w("noImage")}</small></span>
                <span><b>{profile.max_running}</b><small>{w("slots")}</small></span>
                <Status enabled={profile.enabled} ready={ready.ready}
                  detail={ready.missing.length ? w("missingCount", { count: ready.missing.length }) : w("ready")} />
                </button>
              </div>;
            })}</div>

            <button className="wsw-collapse" onClick={() => setShowSystem(!showSystem)}>
              <span><b>{w("systemWorker")}</b><small>{w("systemWorkerNote")}</small></span>
              <span>{showSystem ? "−" : "+"}</span>
            </button>
            {showSystem && system && <CompactRow profile={system} draft={draft} accounts={accounts}
              secrets={secrets} providerSecrets={workspace.provider_secrets || []}
              providerSecretUpdates={providerSecrets} onOpen={() => choose(profileLabel(system))} />}

            <button className="wsw-collapse" onClick={() => setShowAdvanced(!showAdvanced)}>
              <span><b>{w("advancedWorkers")}</b><small>{w("advancedWorkersNote")}</small></span>
              <span>{showAdvanced ? "−" : "+"}</span>
            </button>
            {showAdvanced && <div className="wsw-advanced-list">
              {customs.map((profile) => <CompactRow key={profile.id} profile={profile}
                draft={draft} accounts={accounts} secrets={secrets}
                providerSecrets={workspace.provider_secrets || []} providerSecretUpdates={providerSecrets}
                onOpen={() => choose(profileLabel(profile))} />)}
              <button className="wsw-add" onClick={addCustom}>{w("addManualWorker")}</button>
            </div>}
          </section>

          <div className="wsw-splitter" role="separator" aria-orientation="vertical"
            aria-label={w("resizeSplit")} aria-valuemin={WORKER_SETTINGS_MASTER_MIN}
            aria-valuemax={splitMax} aria-valuenow={masterWidth} tabIndex={0}
            onPointerDown={startResize} onKeyDown={resizeByKeyboard} onDoubleClick={resetSplit}>
            <span />
          </div>

          <section className="wsw-detail">
            <button className="wsw-mobile-back" onClick={() => setCompactEditor(false)}>{w("allDirections")}</button>
            {selectedProfile ? <WorkerEditor profile={selectedProfile} draft={draft}
              accounts={accounts} secrets={secrets} providers={draft.llm_providers || []}
              providerSecretMeta={workspace.provider_secrets || []} providerSecretUpdates={providerSecrets}
              testState={tests[profileLabel(selectedProfile)]}
              onPatch={(patch) => patchProfile(profileLabel(selectedProfile), patch)}
              onSecret={(action, value, baseUrl) => setSecrets((current) => setSecretUpdate(
                current, selectedProfile.credential_account, action, value, baseUrl))}
              onCopy={(source) => {
                const from = draft.worker_profiles.find((profile) => profileLabel(profile) === source);
                if (from) patchProfile(profileLabel(selectedProfile), copyWorkerFields(from, selectedProfile));
              }}
              onCustomize={() => {
                const key = directionForProfile(selectedProfile); if (!key) return;
                const result = cloneRuntimeForDirection(draft.runtime_profiles, key, selectedProfile.runtime);
                setDraft({ ...draft, runtime_profiles: result.runtimes,
                  worker_profiles: draft.worker_profiles.map((profile) =>
                    profileLabel(profile) === profileLabel(selectedProfile)
                      ? { ...profile, runtime: result.runtimeId } : profile) });
              }}
              onTest={(apiKey, validateModel) => void testProfile(selectedProfile, apiKey, validateModel)} />
              : <div className="wsw-empty">{w("selectWorker")}</div>}
          </section>
        </div>}

        {section === "reason" && <ReasonSettings draft={draft} accounts={accounts}
          providers={draft.llm_providers || []} onChange={(value) => {
          setDraft(value); setValidation(null);
        }} />}
        {section === "providers" && <ProviderSettings draft={draft}
          templates={workspace.provider_templates || []} secrets={workspace.provider_secrets || []}
          secretUpdates={providerSecrets} onChange={(value) => { setDraft(value); setValidation(null); setNotice(null); }}
          onSecret={stageProviderSecret} />}
        {auxSection === "runtimes" && <RuntimeTemplates draft={draft} onUpdate={patchRuntime} />}
        {auxSection === "diagnostics" && <Diagnostics draft={draft} validation={validation}
          accounts={accounts} secrets={secrets} providerSecrets={workspace.provider_secrets || []}
          providerSecretUpdates={providerSecrets} tests={tests}
          onValidate={() => void validate()} onTest={(profile) => testProfile(profile)} />}
      </main>
    </div>

    {reviewOpen && <Review validation={validation} errors={errors.length}
      warnings={warnings.length} applying={applying}
      onClose={() => setReviewOpen(false)} onApply={() => void confirmApply()} />}
  </div>;
}

function Status({ enabled, ready, detail }: { enabled: boolean; ready: boolean; detail: string }) {
  const { w } = useWswText();
  return <span className={`wsw-status ${enabled && ready ? "ready" : enabled ? "blocked" : "off"}`}>
    <i />{enabled ? detail : w("disabled")}
  </span>;
}

function CompactRow({ profile, draft, accounts, secrets, providerSecrets, providerSecretUpdates, onOpen }: {
  profile: WorkerProfile; draft: WorkerSettings; accounts: CredentialAccount[];
  secrets: WorkerSecretUpdate[]; providerSecrets: LLMProviderSecretMeta[];
  providerSecretUpdates: ProviderSecretUpdate[]; onOpen: () => void;
}) {
  const { w } = useWswText();
  const ready = workerReadiness(profile, draft.runtime_profiles, accounts, secrets,
    providerSecrets, providerSecretUpdates, draft.llm_providers || []);
  const isSystem = profileLabel(profile) === "pi-worker";
  return <button className={`wsw-compact-row ${isSystem ? "wsw-system-row" : ""}`} onClick={onOpen}>
    <span><b>{profileLabel(profile)}</b><small>{profile.model || w("noModel")}</small></span>
    <Status enabled={profile.enabled} ready={ready.ready}
      detail={ready.ready ? w("ready") : w("incomplete")} />
  </button>;
}

function WorkerEditor({ profile, draft, accounts, secrets, providers, providerSecretMeta,
  providerSecretUpdates, testState, onPatch, onSecret, onCopy, onCustomize, onTest }: {
  profile: WorkerProfile; draft: WorkerSettings; accounts: CredentialAccount[];
  secrets: WorkerSecretUpdate[]; providers: LLMProvider[]; providerSecretMeta: LLMProviderSecretMeta[];
  providerSecretUpdates: ProviderSecretUpdate[]; testState?: TestState;
  onPatch: (patch: Partial<WorkerProfile>) => void;
  onSecret: (action: "retain" | "replace" | "remove", value?: string, baseUrl?: string) => void;
  onCopy: (source: string) => void; onCustomize: () => void; onTest: (apiKey: string, validateModel: boolean) => void;
}) {
  const { w } = useWswText();
  const [keyValue, setKeyValue] = useState("");
  const [copySource, setCopySource] = useState("");
  const label = profileLabel(profile);
  const account = configuredAccount(accounts, profile.credential_account);
  const update = secrets.find((row) => row.account_id === profile.credential_account);
  const runtime = draft.runtime_profiles.find((row) => row.id === profile.runtime);
  const direction = directionForProfile(profile);
  const provider = providers.find((row) => row.id === profile.provider_ref);
  const providerUpdate = providerSecretUpdates.find((row) => row.provider_id === profile.provider_ref);
  const providerMeta = providerSecretMeta.find((row) => row.provider_id === profile.provider_ref);
  const providerHasSecret = providerUpdate?.action === "replace" ? Boolean(providerUpdate.value?.trim())
    : providerUpdate?.action === "remove" ? false : Boolean(providerMeta?.present);
  const providerModels = provider?.models || [];
  const ready = workerReadiness(profile, draft.runtime_profiles, accounts, secrets,
    providerSecretMeta, providerSecretUpdates, providers);
  const endpoint = provider?.base_url || endpointForProfile(profile, accounts);
  const usesProvider = Boolean(profile.provider_ref);
  useEffect(() => setKeyValue(""), [label]);

  const secretState = update?.action === "replace" ? w("replacementStaged")
    : update?.action === "remove" ? w("removalStaged")
      : account ? w("configured") : w("notConfigured");
  const title = direction ? w(`direction.${direction}`) : label;
  const isSystem = label === "pi-worker";
  const kicker = direction ? w("directionWorker")
    : isSystem ? w("systemWorkerKicker") : w("manualWorker");

  return <div className="wsw-editor">
    <div className="wsw-editor-head">
      <div><span className="wsw-kicker">{kicker}</span><h2>{title}</h2>
        <p>{label} · pi · {w("compatible")}</p></div>
      <label className={`wsw-switch wsw-worker-toggle ${isSystem ? "wsw-system-toggle" : ""}`}>
        <input type="checkbox" checked={profile.enabled}
          onChange={(event) => onPatch({ enabled: event.target.checked })} />
        <span className="wsw-switch-pill"><span className="wsw-switch-knob" /></span>
        <b>{profile.enabled ? w("enabled") : w("disabled")}</b>
      </label>
    </div>
    <div className={`wsw-readiness ${ready.ready ? "ready" : "blocked"}`}><i />
      {ready.ready ? w("structurallyReady")
        : w("needs", { items: translatedMissing(w, ready.missing) })}
    </div>

    <Group title={w("groupConnection")} note={w("groupConnectionNote")}>
      <Field label={w("providerRef")}>
        <select value={profile.provider_ref || ""} onChange={(event) => {
          const ref = event.target.value;
          const selectedProvider = providers.find((item) => item.id === ref);
          onPatch({
            provider_ref: ref,
            base_url: "",
            credential_mode: ref ? "provider" : "",
            credential_account: ref || "",
            wire_api: "auto",
            auth_mode: "bearer",
            auth_header: "Authorization",
            auth_prefix: "Bearer",
            model: selectedProvider?.default_model || selectedProvider?.models?.[0] || "",
          });
        }}>
          <option value="">{w("chooseProvider")}</option>
          {providers.map((item) => <option value={item.id} key={item.id}>{item.label || item.id}</option>)}
        </select>
        <small>{usesProvider
          ? `${w("providerSecret")}: ${providerHasSecret ? w("credentialReady") : w("credentialMissing")}`
          : w("providerRequiredNote")}</small>
      </Field>
      {usesProvider ? <div className="wsw-provider-summary">
        <div><span>{w("providerEndpoint")}</span><b>{host(endpoint, w("notConfigured"))}</b></div>
        <div><span>{w("providerProtocol")}</span><b>{provider?.wire_api || w("protocolAuto")}</b></div>
        <div><span>{w("providerModels")}</span><b>{providerModels.length ? w("modelsLoaded", { count: providerModels.length }) : w("notConfigured")}</b></div>
      </div> : <div className="wsw-provider-empty">{w("providerRequiredNote")}</div>}
      <div className="wsw-inline-action">
        <span className={`wsw-provider-pill ${usesProvider && providerHasSecret ? "ready" : "blocked"}`}><i />
          {usesProvider ? (providerHasSecret ? w("credentialReady") : w("credentialMissing")) : w("chooseProvider")}
        </span>
        {usesProvider && <button className="wsw-btn quiet" disabled={testState?.busy || !profile.model}
          onClick={() => onTest("", false)}>{testState?.busy ? w("testing") : w("testConnection")}</button>}
      </div>
    </Group>

    <Group title={w("groupModel")} note={w("groupModelNote")}>
      <Field label={w("modelId")}><input list={`wsw-models-${label}`} value={profile.model || ""}
        placeholder="deepseek-v4-flash" onChange={(event) => onPatch({ model: event.target.value })} />
        <datalist id={`wsw-models-${label}`}>{(providerModels.length ? providerModels.map((id) => ({ id, name: id })) : (testState?.models || [])).map((model) =>
          <option key={model.id} value={model.id}>{model.name}</option>)}</datalist></Field>
      <Field label={w("reasoningEffort")}><select value={profile.effort || "medium"}
        onChange={(event) => onPatch({ effort: event.target.value })}>
        <option value="minimal">{w("minimal")}</option><option value="low">{w("low")}</option>
        <option value="medium">{w("medium")}</option><option value="high">{w("high")}</option>
        <option value="max">{w("max")}</option>
      </select></Field>
      <div className="wsw-inline-action">
        <button className="wsw-btn quiet" disabled={testState?.busy || !profile.model}
          onClick={() => onTest("", true)}>{testState?.busy ? w("testing") : w("validateModel")}</button>
        <small>{w("modelProbeCost")}</small>
      </div>
    </Group>

    <Group title={w("groupRuntime")} note={w("groupRuntimeNote")}>
      <Field label={w("environment")}><select value={profile.runtime}
        onChange={(event) => onPatch({ runtime: event.target.value })}>
        {draft.runtime_profiles.map((item) => <option key={item.id} value={item.id}>
          {item.label} · {item.backend}
        </option>)}
      </select></Field>
      <Field label={w("workerImage")}><input disabled={runtime?.backend === "local"}
        value={profile.image || ""} placeholder="ctf-swarm-pi:0.2.0"
        onChange={(event) => onPatch({ image: event.target.value })} /></Field>
      {direction && BUILTIN_RUNTIME_IDS.has(profile.runtime) &&
        <button className="wsw-btn quiet align-start" onClick={onCustomize}>{w("customizeDirection")}</button>}
      {runtime && !BUILTIN_RUNTIME_IDS.has(runtime.id) && <div className="wsw-private-runtime">
        <b>{runtime.label}</b><span>{runtime.network || w("hostNetwork")} · {
          runtime.memory || w("defaultMemory")} · {runtime.cpus || w("defaultCpu")}</span>
      </div>}
    </Group>

    <Group title={w("groupCapacity")} note={w("groupCapacityNote")}>
      <div className="wsw-two">
        <Field label={w("runSlots")}><input type="number" min={1} max={64}
          value={profile.max_running} onChange={(event) => onPatch({
            max_running: Math.max(1, Number(event.target.value || 1)),
          })} /></Field>
        <Field label={w("reviewSlots")}><input type="number" min={1} max={16}
          value={profile.max_review_running || 1} onChange={(event) => onPatch({
            max_review_running: Math.max(1, Number(event.target.value || 1)),
          })} /></Field>
      </div>
    </Group>

    <Group title={w("groupAdvanced")} note={w("groupAdvancedNote")}>
      <div className="wsw-copy"><select value={copySource}
        onChange={(event) => setCopySource(event.target.value)}>
        <option value="">{w("chooseSource")}</option>
        {draft.worker_profiles.filter((item) => profileLabel(item) !== label).map((item) =>
          <option key={item.id} value={profileLabel(item)}>{profileLabel(item)}</option>)}
      </select><button className="wsw-btn quiet" disabled={!copySource}
        onClick={() => onCopy(copySource)}>{w("copySafe")}</button></div>
      <Field label={w("priority")}><input type="number" value={profile.priority}
        onChange={(event) => onPatch({ priority: Number(event.target.value || 0) })} /></Field>
      {!direction && label !== "pi-worker" &&
        <div className="wsw-manual-note">{w("manualOnlyNote")}</div>}
    </Group>
  </div>;
}

function ProbeLayer({ label, value }: { label: string; value?: { ok: boolean; status?: number | null; attempted?: boolean } }) {
  const skipped = value?.attempted === false;
  const tone = skipped ? "off" : value?.ok ? "ready" : "blocked";
  return <span className={`wsw-probe-layer ${tone}`}><i />{label}
    {value?.status != null ? ` · HTTP ${value.status}` : skipped ? " · —" : ""}</span>;
}

function Group({ title, note, children }: {
  title: string; note: string; children: React.ReactNode;
}) {
  return <section className="wsw-editor-group"><div className="wsw-group-title">
    <h3>{title}</h3><p>{note}</p></div><div className="wsw-fields">{children}</div>
  </section>;
}
function Field({ label, status, children }: {
  label: string; status?: string; children: React.ReactNode;
}) {
  return <label className="wsw-field"><span>{label}{status && <em>{status}</em>}</span>{children}</label>;
}

function RuntimeTemplates({ draft, onUpdate }: {
  draft: WorkerSettings;
  onUpdate: (id: string, patch: Partial<WorkerSettings["runtime_profiles"][number]>) => void;
}) {
  const { w } = useWswText();
  return <section className="wsw-page-section">
    <PageHead kicker={w("runtimeKicker")} title={w("runtimeTitle")} note={w("runtimeNote")} />
    <div className="wsw-runtime-grid">{draft.runtime_profiles.map((runtime) => {
      const builtin = BUILTIN_RUNTIME_IDS.has(runtime.id);
      return <article className="wsw-runtime-card" key={runtime.id}>
        <div className="wsw-card-head"><div>
          <span className="wsw-kicker">{builtin ? w("builtIn") : w("private")}</span>
          <h3>{runtime.label}</h3><code>{runtime.id}</code></div>
          <span className={`wsw-runtime-kind ${runtime.backend}`}>{runtime.backend}</span>
        </div>
        <div className="wsw-runtime-fields">
          <Field label={w("backend")}><select disabled={builtin} value={runtime.backend}
            onChange={(event) => onUpdate(runtime.id, {
              backend: event.target.value as "local" | "container",
            })}><option value="container">{w("container")}</option>
            <option value="local">{w("local")}</option></select></Field>
          <Field label={w("network")}><select disabled={builtin || runtime.backend === "local"}
            value={runtime.network || "bridge"}
            onChange={(event) => onUpdate(runtime.id, { network: event.target.value })}>
            <option value="bridge">{w("bridge")}</option><option value="host">{w("host")}</option>
            <option value="none">{w("offline")}</option></select></Field>
          <Field label={w("memory")}><input disabled={builtin || runtime.backend === "local"}
            value={runtime.memory || ""}
            onChange={(event) => onUpdate(runtime.id, { memory: event.target.value })} /></Field>
          <Field label={w("cpu")}><input disabled={builtin || runtime.backend === "local"}
            value={runtime.cpus || ""}
            onChange={(event) => onUpdate(runtime.id, { cpus: event.target.value })} /></Field>
          <Field label={w("pidLimit")}><input type="number"
            disabled={builtin || runtime.backend === "local"} value={runtime.pids_limit || 0}
            onChange={(event) => onUpdate(runtime.id, {
              pids_limit: Math.max(0, Number(event.target.value || 0)),
            })} /></Field>
        </div>
      </article>;
    })}</div>
  </section>;
}

function PageHead({ kicker, title, note, action }: {
  kicker: string; title: string; note: string; action?: React.ReactNode;
}) {
  return <div className="wsw-heading"><div><span className="wsw-kicker">{kicker}</span>
    <h2>{title}</h2><p>{note}</p></div>{action}</div>;
}


function providerSecretLabel(
  providerId: string,
  saved: LLMProviderSecretMeta[],
  updates: ProviderSecretUpdate[],
  w: WswText,
): string {
  const update = updates.find((row) => row.provider_id === providerId);
  if (update?.action === "replace") return w("replacementStaged");
  if (update?.action === "remove") return w("removalStaged");
  return saved.some((row) => row.provider_id === providerId && row.present) ? w("configured") : w("notConfigured");
}
function providerSecretStatus(
  providerId: string,
  saved: LLMProviderSecretMeta[],
  updates: ProviderSecretUpdate[],
  w: WswText,
): { label: string; tone: "good" | "warn" } {
  const update = updates.find((row) => row.provider_id === providerId);
  const present = update?.action === "replace" || (!update && saved.some((row) => row.provider_id === providerId && row.present));
  return { label: providerSecretLabel(providerId, saved, updates, w), tone: present ? "good" : "warn" };
}


function uniqueProviderId(base: string, providers: LLMProvider[]): string {
  const slug = (base || "custom").toLowerCase().replace(/[^a-z0-9_.-]+/g, "-").replace(/^-+|-+$/g, "") || "custom";
  const used = new Set(providers.map((row) => row.id));
  if (!used.has(slug)) return slug;
  for (let i = 2; i < 100; i += 1) {
    const candidate = `${slug}-${i}`;
    if (!used.has(candidate)) return candidate;
  }
  return `${slug}-${Math.random().toString(36).slice(2, 6)}`;
}

function ProviderSettings({ draft, templates, secrets, secretUpdates, onChange, onSecret }: {
  draft: WorkerSettings; templates: LLMProviderTemplate[]; secrets: LLMProviderSecretMeta[];
  secretUpdates: ProviderSecretUpdate[];
  onChange: (draft: WorkerSettings) => void;
  onSecret: (providerId: string, action: "retain" | "replace" | "remove", value?: string) => void;
}) {
  const { w } = useWswText();
  const [templateId, setTemplateId] = useState(templates[0]?.id || "");
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const [probeState, setProbeState] = useState<Record<string, TestState>>({});
  const providers = draft.llm_providers || [];
  const refs = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const profile of draft.worker_profiles) {
      const ref = String(profile.provider_ref || "").trim();
      if (ref) counts[ref] = (counts[ref] || 0) + 1;
    }
    for (const key of ["planner", "titler"] as const) {
      const ref = String(draft.llm_profiles?.[key]?.provider_ref || "").trim();
      if (ref) counts[ref] = (counts[ref] || 0) + 1;
    }
    return counts;
  }, [draft]);
  const patchProviders = (next: LLMProvider[]) => onChange({ ...draft, llm_providers: next });
  const patchProvider = (index: number, patch: Partial<LLMProvider>) => patchProviders(
    providers.map((provider, i) => i === index ? { ...provider, ...patch } : provider),
  );
  const addProvider = () => {
    const template = templates.find((item) => item.id === templateId) || templates[0] || {
      id: "custom-openai", label: "Custom OpenAI-compatible Relay", base_url: "", wire_api: "auto",
      auth_mode: "bearer", auth_header: "Authorization", auth_prefix: "Bearer", models: [],
    } as LLMProviderTemplate;
    const id = uniqueProviderId(template.id || template.label, providers);
    patchProviders([...providers, { ...template, id, label: template.label || id, models: [...(template.models || [])], default_model: template.default_model || template.models?.[0] || "" }]);
  };
  const removeProvider = (provider: LLMProvider) => {
    const count = refs[provider.id] || 0;
    if (count > 0 && !window.confirm(w("providerReferencedDelete", { count }))) return;
    patchProviders(providers.filter((item) => item.id !== provider.id));
    onSecret(provider.id, "remove");
  };
  const runProbe = async (provider: LLMProvider, validateModel = false) => {
    const id = provider.id;
    setProbeState((current) => ({ ...current, [id]: { busy: true } }));
    const result = await probeLlmProvider(provider, keyDrafts[id] || "", validateModel, provider.default_model || provider.models?.[0] || "");
    setProbeState((current) => ({ ...current, [id]: {
      busy: false, ok: Boolean(result.ok), detail: result.detail || w("connectionFailed"),
      models: result.models, detectedWireApi: result.detected_wire_api, probe: result,
    } }));
    if (result.detected_wire_api && provider.wire_api === "auto") {
      const index = providers.findIndex((item) => item.id === id);
      if (index >= 0) patchProvider(index, { wire_api: result.detected_wire_api });
    }
    if (result.models?.length) {
      const ids = result.models.map((model) => model.id).filter(Boolean);
      const index = providers.findIndex((item) => item.id === id);
      if (index >= 0) patchProvider(index, { models: ids, default_model: provider.default_model || ids[0] || "" });
    }
  };

  return <section className="wsw-page-section">
    <PageHead kicker={w("providerSettingsKicker")} title={w("providerSettingsTitle")} note={w("providerSettingsNote")}
      action={<div className="wsw-provider-create"><select value={templateId}
        onChange={(event) => setTemplateId(event.target.value)}>
        {templates.map((template) => <option key={template.id} value={template.id}>{template.label || template.id}</option>)}
      </select><button className="wsw-btn primary" type="button" onClick={addProvider}>{w("createProvider")}</button></div>} />
    {!providers.length && <div className="wsw-empty">{w("noProviders")}</div>}
    <div className="wsw-provider-grid">{providers.map((provider, index) => {
      const probe = probeState[provider.id];
      const keyValue = keyDrafts[provider.id] || "";
      const modelOptions = provider.models || [];
      const secretStatus = providerSecretStatus(provider.id, secrets, secretUpdates, w);
      return <article className="wsw-runtime-card wsw-provider-card" key={`${provider.id}-${index}`}>
        <div className="wsw-card-head"><div><span className="wsw-kicker">{provider.kind || w("provider")}</span>
          <h3>{provider.label || provider.id}</h3><code>{provider.id}</code></div>
          <span className={`wsw-credential-pill ${secretStatus.tone}`}>
            {secretStatus.label}
          </span></div>
        <div className="wsw-runtime-fields">
          <Field label={w("providerId")}><input value={provider.id}
            onChange={(event) => patchProvider(index, { id: event.target.value.trim() })} /></Field>
          <Field label={w("providerLabel")}><input value={provider.label || ""}
            onChange={(event) => patchProvider(index, { label: event.target.value })} /></Field>
          <Field label={w("baseUrl")}><input value={provider.base_url || ""} placeholder="https://api.example.com/v1"
            onChange={(event) => patchProvider(index, { base_url: event.target.value })} /></Field>
          <Field label={w("wireApi")}><select value={provider.wire_api || "auto"}
            onChange={(event) => patchProvider(index, { wire_api: event.target.value })}>
            <option value="auto">auto</option><option value="openai-chat">openai-chat</option>
            <option value="openai-responses">openai-responses</option><option value="openai">openai</option>
          </select></Field>
          <Field label={w("authMode")}><select value={provider.auth_mode || "bearer"}
            onChange={(event) => patchProvider(index, {
              auth_mode: event.target.value,
              auth_header: event.target.value === "x-api-key" ? "x-api-key" : event.target.value === "bearer" ? "Authorization" : provider.auth_header,
              auth_prefix: event.target.value === "bearer" ? "Bearer" : event.target.value === "x-api-key" ? "" : provider.auth_prefix,
            })}>
            <option value="bearer">Bearer</option><option value="x-api-key">x-api-key</option><option value="custom">{w("authCustom")}</option>
          </select></Field>
          <Field label={w("defaultModel")}><input list={`provider-models-${provider.id}`} value={provider.default_model || ""}
            onChange={(event) => patchProvider(index, { default_model: event.target.value })} />
            <datalist id={`provider-models-${provider.id}`}>{modelOptions.map((model) => <option key={model} value={model} />)}</datalist></Field>
          {provider.auth_mode === "custom" && <>
            <Field label={w("authHeader")}><input value={provider.auth_header || ""}
              onChange={(event) => patchProvider(index, { auth_header: event.target.value })} /></Field>
            <Field label={w("authPrefix")}><input value={provider.auth_prefix || ""}
              onChange={(event) => patchProvider(index, { auth_prefix: event.target.value })} /></Field>
          </>}
          <Field label={w("providerApiKey")} status={secretStatus.label}>
            <div className="wsw-secret-line"><input type="password" autoComplete="new-password"
              value={keyValue} placeholder={w("blankKeepsKey")}
              onChange={(event) => { const value = event.target.value; setKeyDrafts((current) => ({ ...current, [provider.id]: value }));
                onSecret(provider.id, value ? "replace" : "retain", value); }} />
              <button className="wsw-mini danger" type="button" onClick={() => {
                setKeyDrafts((current) => ({ ...current, [provider.id]: "" })); onSecret(provider.id, "remove");
              }}>{w("remove")}</button></div><small>{w("keyRule")}</small>
          </Field>
          <Field label={w("providerModels")}><textarea value={modelOptions.join("\n")} rows={Math.min(6, Math.max(3, modelOptions.length || 3))}
            onChange={(event) => patchProvider(index, { models: event.target.value.split(/\r?\n/).map((x) => x.trim()).filter(Boolean) })} /></Field>
          <Field label={w("notes")}><textarea value={provider.notes || ""} rows={3}
            onChange={(event) => patchProvider(index, { notes: event.target.value })} /></Field>
          <div className="wsw-llm-actions">
            <button className="wsw-btn quiet" type="button" aria-label={w("fetchModels")} disabled={probe?.busy} onClick={() => void runProbe(provider, false)}>{probe?.busy ? w("testing") : w("fetchProviderModels")}</button>
            <button className="wsw-btn quiet" type="button" disabled={probe?.busy || !provider.default_model} onClick={() => void runProbe(provider, true)}>{w("validateModel")}</button>
            <button className="wsw-btn quiet danger" type="button" onClick={() => removeProvider(provider)}>{w("deleteProvider")}</button>
            {!!refs[provider.id] && <small>{w("providerRefs", { count: refs[provider.id] })}</small>}
          </div>
          {probe?.detail && <div className={`wsw-llm-test ${probe.ok ? "good" : "bad"}`}>
            <b>{probe.ok ? w("ready") : w("connectionFailed")}</b><span>{probe.detail}</span>
            {probe.models && <small>{w("modelsLoaded", { count: probe.models.length })}</small>}
          </div>}
        </div>
      </article>;
    })}</div>
  </section>;
}

function ReasonSettings({ draft, accounts, providers, onChange }: {
  draft: WorkerSettings; accounts: CredentialAccount[]; providers: LLMProvider[];
  onChange: (draft: WorkerSettings) => void;
}) {
  const { w } = useWswText();
  const [llmTests, setLlmTests] = useState<Record<LlmProfileKey, { busy?: boolean; result?: ReasonLlmProbe }>>({
    planner: {}, titler: {},
  });
  const review = draft.review_policy || {};
  const reviewOptions = draft.worker_profiles
    .filter((profile) => profile.roles?.includes("review"))
    .map(profileLabel);
  const workerSource = draft.worker_profiles.find((profile) =>
    profile.enabled && (directionForProfile(profile) || profileLabel(profile) === "pi-worker"));
  const accountOptions = accounts.length ? accounts : [{
    account_id: String((draft.llm_profiles.planner.credential_account || "pi-main")),
    engine: "api", mode: "managed", present: false, writable_state: true, details: {},
  } as CredentialAccount];
  const updateReview = (patch: Record<string, unknown>) => onChange({
    ...draft, review_policy: { ...review, ...patch },
  });
  const updateLlm = (key: LlmProfileKey, patch: Partial<WorkerSettings["llm_profiles"][LlmProfileKey]>) => onChange({
    ...draft, llm_profiles: { ...draft.llm_profiles,
      [key]: { ...draft.llm_profiles[key], ...patch } },
  });
  const restoreDeepSeek = (key: LlmProfileKey) => {
    const deepseek = providers.find((item) => /deepseek/i.test(`${item.id} ${item.label}`));
    updateLlm(key, {
      provider_ref: deepseek?.id || "",
      provider: deepseek ? "registry" : "deepseek",
      base_url: deepseek ? "" : DEFAULT_DEEPSEEK_BASE_URL,
      model: deepseek?.default_model || deepseek?.models?.[0]
        || (key === "planner" ? "deepseek-v4-pro" : "deepseek-v4-flash"),
      credential_source: deepseek ? "provider" : "auto",
      credential_account: deepseek?.id || "pi-main",
      wire_api: "auto",
    });
  };
  const copyFromWorker = (key: LlmProfileKey) => {
    if (!workerSource) return;
    updateLlm(key, {
      provider_ref: workerSource.provider_ref || "",
      provider: workerSource.provider_ref ? "registry" : "openai-compatible",
      model: workerSource.model || draft.llm_profiles[key].model,
      base_url: workerSource.provider_ref ? "" : (workerSource.base_url || DEFAULT_DEEPSEEK_BASE_URL),
      effort: workerSource.effort || draft.llm_profiles[key].effort || "medium",
      credential_source: workerSource.provider_ref ? "provider" : "account",
      credential_account: workerSource.provider_ref || workerSource.credential_account || draft.llm_profiles[key].credential_account || "pi-main",
      wire_api: workerSource.wire_api || draft.llm_profiles[key].wire_api || "auto",
    });
  };
  const testLlm = async (key: LlmProfileKey) => {
    const profile = draft.llm_profiles[key];
    setLlmTests((prev) => ({ ...prev, [key]: { busy: true } }));
    const result = await testLlmEndpoint(key, profile);
    setLlmTests((prev) => ({ ...prev, [key]: { busy: false, result } }));
  };
  const planner = draft.llm_profiles.planner;
  const titler = draft.llm_profiles.titler;
  const plannerTest = llmTests.planner.result;
  const titlerTest = llmTests.titler.result;
  const reviewerAccount = accounts.find((account) => account.account_id === String((review as Record<string, unknown>).credential_account || ""));
  const providerById = (id?: string) => providers.find((provider) => provider.id === String(id || ""));
  const reviewProviderRef = String((review as Record<string, unknown>).provider_ref || "").trim();
  const reviewerProvider = providerById(reviewProviderRef);
  const layerLabel = (name: string) => {
    if (name === "base_url") return w("baseUrl");
    if (name === "auth") return w("credentialReady");
    if (name === "models") return w("modelDiscovery");
    if (name === "chat") return w("chatProbe");
    return name;
  };

  return <section className="wsw-page-section">
    <PageHead kicker={w("reasonKicker")} title={w("navReason")} note={w("reasonNote")} />
    <div className="wsw-reason-hero">
      <div><span className="wsw-kicker">{w("relayConfig")}</span>
        <h2>{w("reasonHealthTitle")}</h2>
        <p>{w("reasonHealthNote")}</p></div>
      <div className="wsw-health-grid">
        <div className="wsw-health-card"><span>{w("intentPlanner")}</span>
          <b>{planner.model || w("noModel")}</b>
          <small>{providerById(planner.provider_ref)?.label || plannerTest?.base_url_host || host(planner.base_url || DEFAULT_DEEPSEEK_BASE_URL, "deepseek")}</small></div>
        <div className="wsw-health-card"><span>{w("reviewerAuditPool")}</span>
          <b>{review.enabled ? w("enabled") : w("disabled")}</b>
          <small>{review.engine || "pi-worker"}{reviewerProvider ? ` · ${reviewerProvider.label || reviewerProvider.id}` : reviewerAccount ? ` · ${reviewerAccount.present ? w("credentialReady") : w("credentialMissing")}` : ""}</small></div>
        <div className="wsw-health-card"><span>{w("runTitler")}</span>
          <b>{titler.model || w("noModel")}</b>
          <small>{titlerTest?.ok ? w("ready") : (titlerTest?.code || providerById(titler.provider_ref)?.label || host(titler.base_url || DEFAULT_DEEPSEEK_BASE_URL, "deepseek"))}</small></div>
      </div>
    </div>
    <div className="wsw-reason-grid">
      <article className="wsw-runtime-card wsw-review-card">
        <div className="wsw-card-head"><div><span className="wsw-kicker">{w("sharedReviewer")}</span>
          <h3>{w("systemPolicy")}</h3><p>{w("sharedReviewerNote")}</p></div>
          <label className={`wsw-system-toggle wsw-policy-toggle ${review.enabled ? "is-on" : "is-off"}`}><input type="checkbox" checked={Boolean(review.enabled)}
            onChange={(event) => updateReview({ enabled: event.target.checked })} />
            <span className="wsw-policy-led" aria-hidden="true" />
            <span className="wsw-policy-copy"><b className="wsw-system-label">{review.enabled ? w("enabled") : w("disabled")}</b>
              <small>{w("reviewerAuditPool")}</small></span></label>
        </div>
        <div className="wsw-runtime-fields">
          <Field label={w("providerRef")}><select value={reviewProviderRef}
             onChange={(event) => {
               const ref = event.target.value;
               updateReview({ provider_ref: ref, credential_source: ref ? "provider" : "", credential_account: ref || "", base_url: "", wire_api: "auto" });
             }}>
             <option value="">{w("chooseProvider")}</option>
             {providers.map((item) => <option value={item.id} key={item.id}>{item.label || item.id}</option>)}
           </select><small>{reviewerProvider ? w("providerManagedNote") : w("reviewerProviderNote")}</small></Field><Field label={w("reviewEngine")}><select value={String(review.engine || "pi-worker")}
            onChange={(event) => updateReview({ engine: event.target.value })}>
            {(reviewOptions.length ? reviewOptions : [String(review.engine || "pi-worker")]).map((name) =>
              <option value={name} key={name}>{name}</option>)}
          </select></Field>
          <Field label={w("reviewMaxConcurrent")}><input type="number" min={1}
            value={Number(review.max_concurrent || 1)}
            onChange={(event) => updateReview({ max_concurrent: Number(event.target.value || 1) })} /></Field>
          <Field label={w("afterFruitless")}><input type="number" min={1}
            value={Number(review.after_fruitless_workers || 3)}
            onChange={(event) => updateReview({
              after_fruitless_workers: Number(event.target.value || 1),
            })} /></Field>
          <Field label={w("everyCompleted")}><input type="number" min={1}
            value={Number(review.every_completed_workers || 6)}
            onChange={(event) => updateReview({
              every_completed_workers: Number(event.target.value || 1),
            })} /></Field>
          <Field label={w("cooldownEvents")}><input type="number" min={0}
            value={Number(review.cooldown_events || 8)}
            onChange={(event) => updateReview({ cooldown_events: Number(event.target.value || 0) })} /></Field>
          <Field label={w("reviewTimeout")}><input type="number" min={30}
            value={Number(review.timeout || 420)}
            onChange={(event) => updateReview({ timeout: Number(event.target.value || 30) })} /></Field>
        </div>
      </article>
      {(["planner", "titler"] as const).map((key) => {
        const profile = draft.llm_profiles[key];
        const test = llmTests[key];
        const account = accounts.find((item) => item.account_id === String(profile.credential_account || ""));
        const boundProvider = providerById(profile.provider_ref);
        const modelOptions = boundProvider?.models || [];
        const usesProvider = Boolean(profile.provider_ref);
        const effectiveHost = boundProvider?.label || test.result?.base_url_host || host(profile.base_url || DEFAULT_DEEPSEEK_BASE_URL, "deepseek");
        return <article className="wsw-runtime-card wsw-relay-card" key={key}>
          <div className="wsw-card-head"><div><span className="wsw-kicker">{key === "planner" ? w("plannerRelay") : w("titlerRelay")}</span>
            <h3>{key === "planner" ? w("intentPlanner") : w("runTitler")}</h3>
            <p>{key === "planner" ? w("plannerNote") : w("titlerNote")}</p></div>
            <span className={`wsw-credential-pill ${usesProvider || account?.present ? "good" : "warn"}`}>
              {usesProvider ? w("providerBound") : account?.present ? w("credentialReady") : w("credentialMissing")}
            </span></div>
          <div className="wsw-relay-main">
            <b>{profile.model || w("noModel")}</b><span>{w("effectiveEndpoint")}: {effectiveHost}</span>
          </div>
          <div className="wsw-runtime-fields">
            <Field label={w("providerRef")}><select value={profile.provider_ref || ""}
               onChange={(event) => {
                 const ref = event.target.value;
                 const selectedProvider = providers.find((item) => item.id === ref);
                 updateLlm(key, {
                   provider_ref: ref,
                   provider: ref ? "registry" : "",
                   base_url: "",
                   credential_source: ref ? "provider" : "",
                   credential_account: ref || "",
                   wire_api: "auto",
                   model: selectedProvider?.default_model || selectedProvider?.models?.[0] || "",
                 });
               }}>
               <option value="">{w("chooseProvider")}</option>
               {providers.map((item) => <option key={item.id} value={item.id}>{item.label || item.id}</option>)}
             </select></Field>
             {usesProvider ? <div className="wsw-provider-facts">
               <div><span>{w("providerEndpoint")}</span><b>{host(boundProvider?.base_url || "", w("notConfigured"))}</b></div>
               <div><span>{w("providerProtocol")}</span><b>{boundProvider?.wire_api || w("protocolAuto")}</b></div>
             </div> : <div className="wsw-provider-empty">{w("providerRequiredNote")}</div>}
             <Field label={w("groupModel")}>{modelOptions.length ? <select className="wsw-model-select" value={profile.model}
               onChange={(event) => updateLlm(key, { model: event.target.value })}>
               {modelOptions.map((model) => <option key={model} value={model}>{model}</option>)}
             </select> : <input className="wsw-model-input" value={profile.model}
               onChange={(event) => updateLlm(key, { model: event.target.value })} />}</Field>
             <Field label={w("effort")}><select value={profile.effort || "medium"}
              onChange={(event) => updateLlm(key, { effort: event.target.value })}>
              <option value="low">low</option><option value="medium">medium</option><option value="high">high</option>
            </select></Field>
            <Field label={w("llmTimeout")}><input type="number" min={0}
              value={Number(profile.timeout || (key === "planner" ? 120 : 60))}
              onChange={(event) => updateLlm(key, { timeout: Number(event.target.value || 0) })} /></Field>
            <div className="wsw-llm-actions">
              <button type="button" className="wsw-btn quiet" onClick={() => restoreDeepSeek(key)}>{w("restoreDeepSeek")}</button>
              <button type="button" className="wsw-btn quiet" disabled={!workerSource}
                onClick={() => copyFromWorker(key)}>{w("copyFromWorker")}</button>
              <button type="button" className="wsw-btn primary" disabled={test?.busy}
                onClick={() => void testLlm(key)}>{test?.busy ? w("testing") : w("testConnection")}</button>
            </div>
            {test.result && <div className={`wsw-llm-test ${test.result.ok ? "good" : "bad"}`}>
              <b>{test.result.ok ? w("ready") : (test.result.code || w("connectionFailed"))}</b>
              <span>{test.result.detail || test.result.model}</span>
              {!!test.result.layers?.length && <div className="wsw-probe-stack">
                {test.result.layers.map((layer, index) => <div className={`wsw-probe-row ${layer.ok ? "good" : "bad"}`} key={`${layer.name}-${index}`}>
                  <i /> <b>{layerLabel(layer.name)}</b>
                  <span>{layer.status ? `${layer.status} · ` : ""}{layer.detail || (layer.attempted === false ? w("optional") : "")}</span>
                </div>)}
              </div>}
            </div>}
          </div>
        </article>;
      })}
    </div>
    <datalist id="wsw-llm-models">
      <option value="gpt-5.5" /><option value="gpt-5.6" />
      <option value="deepseek-v4-pro" /><option value="deepseek-v4-flash" />
      <option value="deepseek-chat" /><option value="deepseek-reasoner" />
    </datalist>
  </section>;
}

function Diagnostics({ draft, validation, accounts, secrets, providerSecrets, providerSecretUpdates, tests, onValidate, onTest }: {
  draft: WorkerSettings; validation: WorkerSettingsValidation | null;
  accounts: CredentialAccount[]; secrets: WorkerSecretUpdate[];
  providerSecrets: LLMProviderSecretMeta[]; providerSecretUpdates: ProviderSecretUpdate[];
  tests: Record<string, TestState>; onValidate: () => void;
  onTest: (profile: WorkerProfile) => Promise<void>;
}) {
  const { w } = useWswText();
  const profiles = draft.worker_profiles.filter((profile) =>
    directionForProfile(profile) || profileLabel(profile) === "pi-worker");
  const errorCount = validation?.issues.filter((issue) => issue.severity === "error").length || 0;
  const warningCount = validation?.issues.filter((issue) => issue.severity === "warning").length || 0;
  return <section className="wsw-page-section">
    <PageHead kicker={w("diagnosticsKicker")} title={w("diagnosticsTitle")}
      note={w("diagnosticsNote")} action={<button className="wsw-btn primary"
        onClick={onValidate}>{w("validateDraft")}</button>} />
    {validation && <div className={`wsw-diagnostic-summary ${validation.ok ? "good" : "bad"}`}>
      <b>{validation.ok ? w("draftValid") : w("draftErrors")}</b>
      <span>{w("validationSummary", { errors: errorCount, warnings: warningCount,
        changes: validation.changes.length })}</span>
    </div>}
    <div className="wsw-diagnostic-list">{profiles.map((profile) => {
      const ready = workerReadiness(profile, draft.runtime_profiles, accounts, secrets,
        providerSecrets, providerSecretUpdates, draft.llm_providers || []);
      const test = tests[profileLabel(profile)];
      const staged = secrets.some((secret) => secret.account_id === profile.credential_account);
      return <article key={profile.id}><div><b>{profileLabel(profile)}</b><span>{
        profile.enabled ? ready.ready ? w("readyForTest")
          : w("missingItems", { items: translatedMissing(w, ready.missing) })
          : w("disabled")}</span>
        {test?.detail && <small className={test.ok ? "good" : "bad"}>{test.detail}</small>}
      </div><button className="wsw-btn quiet"
        disabled={!profile.enabled || !ready.ready || test?.busy || staged}
        onClick={() => void onTest(profile)}>{test?.busy ? w("testing") : w("testConnection")}</button>
      </article>;
    })}</div>
  </section>;
}

function Review({ validation, errors, warnings, applying, onClose, onApply }: {
  validation: WorkerSettingsValidation | null; errors: number; warnings: number;
  applying: boolean; onClose: () => void; onApply: () => void;
}) {
  const { lang, w } = useWswText();
  return <div className="wsw-review-backdrop" onMouseDown={() => !applying && onClose()}>
    <section className="wsw-review" role="dialog" aria-modal="true"
      aria-labelledby="wsw-review-title" onMouseDown={(event) => event.stopPropagation()}>
      <div className="wsw-review-head"><div><span className="wsw-kicker">{w("atomicApply")}</span>
        <h2 id="wsw-review-title">{w("reviewTitle")}</h2></div>
        <button onClick={onClose} disabled={applying} aria-label={w("keepEditing")}>×</button></div>
      <div className="wsw-review-summary">
        <span className={errors ? "bad" : "good"}>{w("errorsCount", { count: errors })}</span>
        <span className={warnings ? "warn" : "good"}>{w("warningsCount", { count: warnings })}</span>
        <span>{w("changesCount", { count: validation?.changes.length || 0 })}</span>
      </div>
      <div className="wsw-review-scroll">
        {!!validation?.issues.length && <div className="wsw-review-block">
          <h3>{w("validation")}</h3>{validation.issues.map((issue, index) =>
            <div className={`wsw-issue ${issue.severity}`} key={`${issue.path}-${index}`}>
              <b>{w(issue.severity)}</b><span>{workerSettingsIssueMessage(lang, issue.code, issue.message)}
                <small>{issue.path}</small></span>
            </div>)}
        </div>}
        <div className="wsw-review-block"><h3>{w("changes")}</h3>
          {validation?.changes.length ? validation.changes.map((change, index) =>
            <div className="wsw-change" key={`${change.scope}-${change.id}-${index}`}>
              <span>{change.scope}</span><b>{change.id}</b><small>{change.fields.join(" · ")}</small>
            </div>) : <p>{w("noChanges")}</p>}
        </div>
        <div className="wsw-review-rule"><b>{w("snapshotGuarantee")}</b>
          <span>{w("snapshotGuaranteeNote")}</span></div>
      </div>
      <div className="wsw-review-actions">
        <button className="wsw-btn quiet" onClick={onClose} disabled={applying}>{w("keepEditing")}</button>
        <button className="wsw-btn primary" disabled={!validation?.ok || applying}
          onClick={onApply}>{applying ? w("applying") : w("applyConfiguration")}</button>
      </div>
    </section>
  </div>;
}
