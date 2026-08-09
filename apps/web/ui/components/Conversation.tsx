"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { getWorkerSettings, checkAuth, SavedFile } from "@/lib/useRun";
import { useT } from "@/lib/i18n";
import { readKey, writeKey } from "@/lib/storage";
import { Icon, type IconName } from "@/components/Icon";

/**
 * The DRAFT surface (docs/07 Phase 4/6): the Command-center shell renders this
 * only before a run exists — the welcome hero plus the dispatch composer. Once
 * a run starts, the TopBar / Decision Timeline / Swarm Inspector / Operator
 * Command Bar take over and this component unmounts, so it deliberately holds
 * no started-run state (the old status hero / HITL thread / run inspector
 * branches were retired with the conversation-first layout; the HITL card now
 * lives in components/HitlCard.tsx for the Swarm Inspector's attention block).
 */

export interface DispatchOpts {
  webSearch: boolean;
  mode: "ctf" | "pentest";
  goal?: string;
  scope?: string;
  // collect mode only controls multi-flag collection. Flag format is independent:
  // default brace regex, or explicit token mode for bare-password ladders.
  collect?: boolean;
  flagFormat?: "brace" | "token" | "custom";
  flagWrapper?: string;
  // optional flag count for collect mode: >0 → stop after collecting that many
  // distinct flags; blank/0 → unknown count, collect until the operator stops.
  collectCount?: number;
  // worker isolation: when true, the run uses a controlled Docker runtime that
  // can't read the host challenge-source tree. Default false = host subprocess.
  containerMode?: boolean;
  wallClockBudget?: number;
  maxTotalWorkers?: number;
  costBudgetUsd?: number;
}

// max height the dispatch textarea auto-grows to (~6–7 rows) before it scrolls
// internally; mirrored by `.composer2 textarea { max-height }` in globals.css.
const DISPATCH_MAX_H = 180;

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function Composer({
  onDispatch,
  attachments,
  onAddFiles,
  onRemoveFile,
}: {
  onDispatch: (prompt: string, opts: DispatchOpts) => void;
  attachments: SavedFile[];
  onAddFiles: (files: FileList | File[]) => void;
  onRemoveFile: (path: string) => void;
}) {
  const t = useT();
  const [text, setText] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  // the dispatch textarea — Cmd/Ctrl+K (palette "focus composer") and the bare
  // "/" shortcut both focus this field.
  const dispatchRef = useRef<HTMLTextAreaElement>(null);

  // Auto-grow: the dispatch textarea expands with its content from its 1-row
  // default up to DISPATCH_MAX_H, then scrolls internally. Measured by resetting
  // height to "auto" (so scrollHeight reflects content, not the current box) and
  // capping the result. Driven from a layout effect on `text` so it also resets
  // after a dispatch clears the field. CSS keeps max-height in sync for the cap.
  const autoGrow = useCallback(() => {
    const el = dispatchRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, DISPATCH_MAX_H)}px`;
  }, []);
  // re-measure on every text change (typing, paste, and the reset-to-empty after
  // dispatch) and on first mount of the dispatch composer.
  useLayoutEffect(() => { autoGrow(); }, [text, autoGrow]);

  // Global composer focus shortcut: bare "/" (when not already typing in a field)
  // jumps focus to the composer. Guarded so it never hijacks typing inside an
  // input/textarea/select/contenteditable, leaving Enter=submit and Shift+Enter=
  // newline untouched. NOTE: Cmd/Ctrl+K is OWNED by the command palette (page.tsx)
  // now — it no longer focuses the composer here. The palette still offers a
  // "focus composer" action (and "/" stays) so the affordance isn't lost.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      const typing = !!el && (
        el.tagName === "INPUT" || el.tagName === "TEXTAREA" ||
        el.tagName === "SELECT" || el.isContentEditable
      );
      const slash = e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey && !typing;
      if (!slash) return;
      const field = dispatchRef.current;
      if (!field) return;
      e.preventDefault();
      field.focus();
      field.select?.();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const [dragOver, setDragOver] = useState(false);
  const [webSearch, setWebSearch] = useState(true);
  const [mode, setMode] = useState<"ctf" | "pentest">("ctf");
  const [goal, setGoal] = useState("");
  const [scope, setScope] = useState("");
  const [collect, setCollect] = useState(false);
  const [collectCount, setCollectCount] = useState("");  // "" = unknown count
  const [flagFormat, setFlagFormat] = useState<"brace" | "token" | "custom">("brace");
  const [flagWrapper, setFlagWrapper] = useState("");
  const [containerMode, setContainerMode] = useState(false);
  // P2-v3: when the control plane runs inside a container, local worker mode is
  // rejected server-side — force container mode and lock the toggle.
  const [containerLocked, setContainerLocked] = useState(false);
  const [wallClockBudget, setWallClockBudget] = useState("0");
  const [maxTotalWorkers, setMaxTotalWorkers] = useState("0");
  const [costBudgetUsd, setCostBudgetUsd] = useState("0");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  useEffect(() => {
    let cancelled = false;
    try {
      const saved = readKey("dswarm.webSearch");
      if (saved === "0") setWebSearch(false);
      const m = readKey("dswarm.mode");
      if (m === "pentest") setMode("pentest");
      if (readKey("dswarm.collect") === "1") setCollect(true);
      const savedFlagFormat = readKey("dswarm.flagFormat");
      if (savedFlagFormat === "token" || savedFlagFormat === "custom") setFlagFormat(savedFlagFormat);
      const savedFlagWrapper = readKey("dswarm.flagWrapper");
      if (savedFlagWrapper) setFlagWrapper(savedFlagWrapper);
      const savedContainer = readKey("dswarm.containerMode");
      if (savedContainer === "1") setContainerMode(true);
      else if (savedContainer !== "0") {
        getWorkerSettings().then((c) => {
          if (!cancelled && c?.worker_backend === "container") setContainerMode(true);
        });
      }
    } catch { /* ignore */ }
    // P2-v3: ask the backend whether IT runs in a container. If so, container
    // mode is mandatory — force it on and lock the toggle (local is server-side
    // rejected, so a local toggle would only produce confusing 400s).
    checkAuth().then((a) => {
      if (!cancelled && a?.inContainer) { setContainerMode(true); setContainerLocked(true); }
    }).catch(() => { /* ignore */ });
    return () => { cancelled = true; };
  }, []);
  const toggleWeb = () => setWebSearch((v) => {
    const nv = !v;
    writeKey("dswarm.webSearch", nv ? "1" : "0");
    return nv;
  });
  const toggleCollect = () => setCollect((v) => {
    const nv = !v;
    writeKey("dswarm.collect", nv ? "1" : "0");
    return nv;
  });
  const pickFlagFormat = (fmt: "brace" | "token" | "custom") => {
    setFlagFormat(fmt);
    writeKey("dswarm.flagFormat", fmt);
  };
  const updateFlagWrapper = (value: string) => {
    setFlagWrapper(value);
    writeKey("dswarm.flagWrapper", value);
  };
  const toggleContainer = () => {
    if (containerLocked) return;  // P2-v3: forced on inside a container
    setContainerMode((v) => {
      const nv = !v;
      writeKey("dswarm.containerMode", nv ? "1" : "0");
      return nv;
    });
  };
  const pickMode = (m: "ctf" | "pentest") => {
    setMode(m);
    writeKey("dswarm.mode", m);
  };

  const dispatch = () => {
    const v = text.trim();
    if (!v) return;
    const optionalInt = (raw: string) => {
      const parsed = parseInt(raw, 10);
      return Number.isNaN(parsed) ? undefined : parsed;
    };
    const optionalFloat = (raw: string) => {
      const parsed = parseFloat(raw);
      return Number.isNaN(parsed) ? undefined : parsed;
    };
    const runCaps = {
      wallClockBudget: optionalInt(wallClockBudget),
      maxTotalWorkers: optionalInt(maxTotalWorkers),
      costBudgetUsd: optionalFloat(costBudgetUsd),
    };
    onDispatch(v, mode === "pentest"
      ? { webSearch, mode, goal: goal.trim(), scope: scope.trim(), containerMode, ...runCaps }
      : { webSearch, mode: "ctf", collect, containerMode,
          flagFormat,
          flagWrapper: flagFormat === "custom" ? flagWrapper.trim() : undefined,
          collectCount: collect ? (parseInt(collectCount, 10) || 0) : undefined,
          ...runCaps });
    setText("");
  };

  return (
    <div className="composer2 motion-run-enter">
      <div
        className={`wrap ${dragOver ? "dragover" : ""}`}
        onDragOver={(e) => {
          // only react to file drags, not text/element drags within the page
          if (!Array.from(e.dataTransfer.types || []).includes("Files")) return;
          e.preventDefault();
          if (!dragOver) setDragOver(true);
        }}
        onDragLeave={(e) => {
          // ignore leaves into descendants (mode-row, textarea, overlay) — only
          // clear when the pointer actually exits the .wrap, else the overlay flickers
          if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
          setDragOver(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          if (e.dataTransfer.files?.length) onAddFiles(e.dataTransfer.files);
        }}
      >
        {dragOver && (
          <div className="drop-overlay" aria-hidden="true">
            <span className="drop-ico"><Icon name="upload" size={26} /></span>
            <span className="drop-label">{t("composer.dropHint")}</span>
          </div>
        )}
        <div className="mode-row">
          <div className="mode-seg" role="tablist" aria-label={t("composer.mode")}>
            <button
              type="button" role="tab" aria-selected={mode === "ctf"}
              className={mode === "ctf" ? "on" : ""}
              title={t("composer.modeCtfTitle")}
              onClick={() => pickMode("ctf")}
            >{t("composer.modeCtf")}</button>
            <button
              type="button" role="tab" aria-selected={mode === "pentest"}
              className={mode === "pentest" ? "on" : ""}
              title={t("composer.modePentestTitle")}
              onClick={() => pickMode("pentest")}
            >{t("composer.modePentest")}</button>
          </div>
        </div>
        <textarea
          ref={dispatchRef}
          data-composer-input
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onInput={autoGrow}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); dispatch(); } }}
          onPaste={(e) => {
            // Paste-to-attach: if the clipboard carries files (e.g. a screenshot,
            // a pcap, a binary), attach them instead of dumping bytes/text. Check
            // `files` first, then fall back to `items` (some browsers surface a
            // pasted image only as a kind:"file" item). Plain text falls through.
            const cd = e.clipboardData;
            const fromItems = Array.from(cd.items || [])
              .filter((it) => it.kind === "file")
              .map((it) => it.getAsFile())
              .filter((f): f is File => f != null);
            if (cd.files?.length) { e.preventDefault(); onAddFiles(cd.files); }
            else if (fromItems.length) { e.preventDefault(); onAddFiles(fromItems); }
          }}
          placeholder={t(mode === "pentest" ? "composer.pentestPlaceholder" : "composer.dispatchPlaceholder")}
        />
        {mode === "pentest" && (
          <div className="pentest-fields">
            <input className="pf-input" value={goal} onChange={(e) => setGoal(e.target.value)} placeholder={t("composer.goalPlaceholder")} />
            <input className="pf-input" value={scope} onChange={(e) => setScope(e.target.value)} placeholder={t("composer.scopePlaceholder")} />
          </div>
        )}
        {attachments.length > 0 && (
          <div className="attach-row">
            {attachments.map((f) => (
              <span className="attach-chip" key={f.path}>
                <span className="afn"><Icon name="paperclip" size={13} /> {f.name}</span>
                <span className="asz">{fmtSize(f.size)}</span>
                <button className="arm" onClick={() => onRemoveFile(f.path)} title={t("composer.removeFile")} aria-label={t("composer.removeFile")}><Icon name="x" size={13} /></button>
              </span>
            ))}
          </div>
        )}
        <div className="crow">
          <button type="button" className="attach-btn" onClick={() => fileRef.current?.click()} title={t("composer.attach")} aria-label={t("composer.attach")}><Icon name="paperclip" /></button>
          <input ref={fileRef} type="file" multiple style={{ display: "none" }}
            onChange={(e) => {
              // Snapshot to a static array BEFORE resetting value: onAddFiles is
              // async (it may await newRun() on a draft), and `value = ""` clears
              // the live FileList mid-flight — leaving the upload with 0 files and
              // a 422 from the endpoint. Array.from() copies the File refs first.
              const picked = e.target.files ? Array.from(e.target.files) : [];
              e.target.value = "";
              if (picked.length) onAddFiles(picked);
            }} />
          <span className="auto-note"><b>▸</b> {t("composer.autoNote")}</span>
          <span className="spacer" />
          {mode === "ctf" && (
            <button
              type="button"
              className={`websearch-toggle ${collect ? "on" : "off"}`}
              onClick={toggleCollect}
              aria-pressed={collect}
              title={t("composer.collectTitle")}
            >
              <Icon name={collect ? "target" : "flag"} size={14} />
              {collect ? t("composer.collectOn") : t("composer.collectOff")}
            </button>
          )}
          {mode === "ctf" && collect && (
            <input
              type="number" min={0} className="collect-count"
              value={collectCount}
              onChange={(e) => setCollectCount(e.target.value)}
              placeholder={t("composer.collectCountPlaceholder")}
              title={t("composer.collectCountTitle")}
            />
          )}
          <button
            type="button"
            className={`websearch-toggle ${webSearch ? "on" : "off"}`}
            onClick={toggleWeb}
            aria-pressed={webSearch}
            title={t(webSearch ? "composer.webOnTitle" : "composer.webOffTitle")}
          >
            <Icon name={webSearch ? "globe" : "lock"} size={14} />
            {webSearch ? t("composer.webOn") : t("composer.webOff")}
          </button>
          <button
            type="button"
            className={`websearch-toggle ${containerMode ? "on" : "off"}${containerLocked ? " locked" : ""}`}
            onClick={toggleContainer}
            aria-pressed={containerMode}
            disabled={containerLocked}
            title={containerLocked
              ? t("composer.containerLockedTitle")
              : t(containerMode ? "composer.containerOnTitle" : "composer.containerOffTitle")}
          >
            <Icon name={containerMode ? "lock" : "globe"} size={14} />
            {containerMode ? t("composer.containerOn") : t("composer.containerOff")}
          </button>
          <button
            type="button"
            className={`advanced-toggle ${advancedOpen ? "on" : ""}`}
            onClick={() => setAdvancedOpen((v) => !v)}
            aria-expanded={advancedOpen}
            aria-controls="dispatch-advanced-controls"
            title={t("composer.advancedTitle")}
          >
            <Icon name="gear" size={14} />
            {t("composer.advanced")}
            <Icon name="chevronDown" size={13} className="advanced-chevron" />
          </button>
          <button className="send" onClick={dispatch} disabled={!text.trim()} title={t("composer.dispatchTitle")} aria-label={t("composer.dispatchTitle")}><Icon name="send" size={15} /></button>
        </div>
        {advancedOpen && (
          <div id="dispatch-advanced-controls" className="composer-advanced-panel">
            {mode === "ctf" && (
              <label className="advanced-field flag-format-field">
                <span>{t("composer.flagFormat")}</span>
                <div className="flag-format-controls">
                  <select
                    className="advanced-select"
                    value={flagFormat}
                    onChange={(e) => {
                      const v = e.target.value;
                      pickFlagFormat(v === "token" ? "token" : v === "custom" ? "custom" : "brace");
                    }}
                    title={t("composer.flagFormatTitle")}
                  >
                    <option value="brace">{t("composer.flagFormatBrace")}</option>
                    <option value="custom">{t("composer.flagFormatCustom")}</option>
                    <option value="token">{t("composer.flagFormatToken")}</option>
                  </select>
                  {flagFormat === "custom" && (
                    <input
                      className="flag-wrapper-input"
                      value={flagWrapper}
                      onChange={(e) => updateFlagWrapper(e.target.value)}
                      placeholder={t("composer.flagWrapperPlaceholder")}
                      title={t("composer.flagWrapperTitle")}
                    />
                  )}
                </div>
              </label>
            )}
            <div className="advanced-metrics-grid">
              <label className="advanced-field advanced-metric-field">
                <span>{t("composer.wallBudget")}</span>
                <input className="collect-count" type="number" min={0} value={wallClockBudget}
                  onChange={(e) => setWallClockBudget(e.target.value)}
                  title={t("composer.wallBudgetTitle")} />
              </label>
              <label className="advanced-field advanced-metric-field">
                <span>{t("composer.maxTotalWorkers")}</span>
                <input className="collect-count" type="number" min={0} value={maxTotalWorkers}
                  onChange={(e) => setMaxTotalWorkers(e.target.value)}
                  title={t("composer.maxTotalWorkersTitle")} />
              </label>
              <label className="advanced-field advanced-metric-field">
                <span>{t("composer.costBudget")}</span>
                <input className="collect-count" type="number" min={0} step="0.01" value={costBudgetUsd}
                  onChange={(e) => setCostBudgetUsd(e.target.value)}
                  title={t("composer.costBudgetTitle")} />
              </label>
            </div>
          </div>
        )}
      </div>
      <div className="hintline">
        {t("composer.hintline")}
        <span className="kbd-hint" aria-label={t("composer.focusHint")}>
          <kbd>{t("composer.focusKey")}</kbd> {t("composer.focusHint")}
        </span>
        <span className="kbd-hint" aria-label={t("palette.hint")}>
          <kbd>{t("palette.key")}</kbd> {t("palette.hint")}
        </span>
      </div>
    </div>
  );
}

/** First-run hero: the wordmark, the one-line pitch, and a row of example
 *  cards that hint at the kinds of challenges the swarm takes. The cards are
 *  presentational (the composer below is the single dispatch surface — kept
 *  untouched), so they read as "here's what this does", not dead buttons. */
const WELCOME_EXAMPLES: { key: string; icon: IconName }[] = [
  { key: "ex1", icon: "globe" },
  { key: "ex2", icon: "lock" },
  { key: "ex3", icon: "target" },
];

function Welcome({ t }: { t: (k: string) => string }) {
  return (
    <div className="welcome">
      <div className="welcome-hero">
        <div className="wm"><span>D-Swarm</span></div>
        <div className="sub">
          {t("welcome.sub")}<code>{t("welcome.subCode")}</code>{t("welcome.subTail")}
        </div>
      </div>
      <div className="suggest-label">{t("welcome.examplesLabel")}</div>
      <div className="suggest">
        {WELCOME_EXAMPLES.map((ex) => (
          <div className="suggest-card" key={ex.key}>
            <span className="s-ico" aria-hidden="true"><Icon name={ex.icon} size={16} /></span>
            <span className="s-cat">{t(`welcome.${ex.key}.cat`)}</span>
            <span className="s-nm">{t(`welcome.${ex.key}.nm`)}</span>
            <span className="s-tg">{t(`welcome.${ex.key}.tg`)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function Conversation({
  onDispatch,
  attachments,
  onAddFiles,
  onRemoveFile,
}: {
  onDispatch: (prompt: string, opts: DispatchOpts) => void;
  attachments: SavedFile[];
  onAddFiles: (files: FileList | File[]) => void;
  onRemoveFile: (path: string) => void;
}) {
  const t = useT();
  return (
    <div className="convo">
      <div className="convo-body">
        <div className="convo-mainpane">
          <div className="convo-scroll">
            <Welcome t={t} />
          </div>
          <Composer
            onDispatch={onDispatch}
            attachments={attachments}
            onAddFiles={onAddFiles}
            onRemoveFile={onRemoveFile}
          />
        </div>
      </div>
    </div>
  );
}
