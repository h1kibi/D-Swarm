import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const css = readFileSync(join(process.cwd(), "app", "globals.css"), "utf8");

function lastRule(selector: string) {
  const start = css.lastIndexOf(selector);
  expect(start).toBeGreaterThanOrEqual(0);
  const end = css.indexOf("}", start);
  expect(end).toBeGreaterThan(start);
  return css.slice(start, end + 1);
}

function ruleContaining(selector: string, text: string) {
  let cursor = 0;
  while (cursor < css.length) {
    const start = css.indexOf(selector, cursor);
    if (start < 0) break;
    const end = css.indexOf("}", start);
    expect(end).toBeGreaterThan(start);
    const rule = css.slice(start, end + 1);
    if (rule.includes(text)) return rule;
    cursor = end + 1;
  }
  throw new Error(`No ${selector} rule contains ${text}`);
}

describe("modern visual system tokens", () => {
  it("defines the glass, glow, and focus primitives used by the command deck chrome", () => {
    expect(css).toContain("--surface-glass");
    expect(css).toContain("--surface-glass-strong");
    expect(css).toContain("--glow-accent");
    expect(css).toContain("--glow-cyan");
    expect(css).toContain("--ring-focus");
  });

  it("adds non-interactive atmospheric layers behind the command-center shell", () => {
    expect(css).toContain(".cc-shell::before");
    expect(css).toContain(".cc-shell::after");
    expect(css).toContain("pointer-events: none");
  });

  it("uses modern glass and accessible focus treatments on high-traffic surfaces", () => {
    expect(css).toContain("backdrop-filter");
    expect(css).toContain("button:focus-visible");
    expect(css).toContain("input:focus-visible");
    expect(css).toContain("textarea:focus-visible");
    expect(css).toContain("select:focus-visible");
  });
});

describe("elevated command-deck surfaces", () => {
  it("keeps the premium chrome primitives for the deck, rail, and settings workspace", () => {
    expect(css).toContain("--surface-elevated");
    expect(css).toContain("--edge-highlight");
    expect(css).toContain("--brand-gradient");
    expect(css).toContain(".topbar::after");
    expect(css).toContain(".thread-item::before");
    expect(css).toContain(".wsw-shell::before");
  });
});

describe("adaptive layout and Worker strategy controls", () => {
  it("keeps roomier deck/window sizing hooks and a polished system Worker enable control", () => {
    expect(css).toContain("--layout-gap-compact");
    expect(css).toContain(".cc-body.layout-refined");
    expect(css).toContain(".wsw-workers.layout-balanced");
    expect(css).toContain(".wsw-system-row");
    expect(css).toContain(".wsw-system-toggle");
    expect(css).toContain(".wsw-switch-pill");
    expect(css).toContain(".wsw-switch-knob");
  });
});

describe("Worker settings system policy toggle markup", () => {
  const source = readFileSync(join(process.cwd(), "components", "WorkerSettingsWorkspace.tsx"), "utf8");

  it("uses the compact status-led switch for the shared system Worker policy", () => {
    expect(source).toContain('wsw-system-toggle wsw-policy-toggle');
    expect(source).not.toContain('wsw-switch wsw-system-toggle wsw-policy-toggle');
    expect(source).toContain('review.enabled ? "is-on" : "is-off"');
    expect(source).toContain('className="wsw-policy-led"');
    expect(source).toContain('className="wsw-policy-copy"');
    expect(source).toContain('className="wsw-system-label"');
  });
});

describe("main deck layout regression guards", () => {
  const page = readFileSync(join(process.cwd(), "app", "page.tsx"), "utf8");
  const inspector = readFileSync(join(process.cwd(), "components", "SwarmInspector.tsx"), "utf8");

  it("keeps the desktop command deck tight below the native title bar", () => {
    expect(css).toContain("--desktop-titlebar-gap: 10px;");
    expect(css).toMatch(/\.shell\.cc-shell\s*\{[\s\S]*?height:\s*100dvh;[\s\S]*?padding-top:\s*var\(--desktop-titlebar-gap\);/);
    expect(css).toMatch(/\.cc-shell\s*>\s*:not\(\.skip-link\)\s*\{[\s\S]*?position:\s*relative;[\s\S]*?z-index:\s*1;/);
    expect(css).toMatch(/\.topbar\s*\{[\s\S]*?margin:\s*0 var\(--layout-edge-pad\);[\s\S]*?padding:\s*2px 12px;[\s\S]*?min-height:\s*34px;[\s\S]*?border-radius:\s*14px;/);
    expect(css).not.toContain("margin: 10px 12px 0;");
    expect(css).not.toContain("margin: 0 12px 0;");
    expect(css).not.toContain("border-radius: var(--radius-shell) var(--radius-shell) 15px 15px;");
    expect(css).toContain("padding: 6px var(--layout-edge-pad) var(--layout-edge-pad);");
    expect(css).not.toContain(`.cc-body.layout-refined {
  gap: var(--layout-gap-compact);
  padding: var(--layout-edge-pad);`);
  });

  it("keeps the right swarm inspector width visibly resizable", () => {
    expect(page).toContain("const INSPECTOR_WIDTH_MAX = 640;");
    expect(page).toContain("Math.round(viewportWidth * 0.46)");
    expect(page).toContain("onResize={onInspectorResize}");
    expect(inspector).toContain("swarm-resizer");
    expect(inspector).toContain("aria-valuenow={width}");
    expect(css).toContain(".swarm-resizer::after");
  });


  it("moves runtime templates and diagnostics out of the crowded topbar into an advanced sidebar group", () => {
    const source = readFileSync(join(process.cwd(), "components", "WorkerSettingsWorkspace.tsx"), "utf8");
    const topbarSource = source.slice(source.indexOf('<header className="wsw-topbar">'), source.indexOf('<div className="wsw-body">'));
    const navSource = source.slice(source.indexOf('<nav className="wsw-nav">'), source.indexOf('<main className="wsw-content">'));
    expect(topbarSource).not.toContain('wsw-aux-controls');
    expect(topbarSource).not.toContain('wsw-aux-btn');
    expect(navSource).toContain('wsw-nav-advanced');
    expect(navSource).toContain('wsw-nav-secondary');
    expect(navSource).toContain('setAuxSection(id)');
    expect(css).toContain('.wsw-nav-advanced');
    expect(css).toContain('.wsw-nav-secondary');
  });

  it("names the auxiliary Worker settings pages as clear advanced tools", () => {
    const source = readFileSync(join(process.cwd(), "components", "WorkerSettingsWorkspace.tsx"), "utf8");
    const text = readFileSync(join(process.cwd(), "lib", "workerSettingsText.ts"), "utf8");
    expect(source).toContain('navAdvanced');
    expect(text).toContain('运行环境');
    expect(text).toContain('配置诊断');
    expect(text).not.toContain('运行时模板');
  });


  it("renders advanced Worker tools in the main content area instead of an unstyled overlay", () => {
    const source = readFileSync(join(process.cwd(), "components", "WorkerSettingsWorkspace.tsx"), "utf8");
    const mainSource = source.slice(source.indexOf('<main className="wsw-content">'), source.indexOf('</main>'));
    expect(mainSource).toContain('auxSection === "runtimes"');
    expect(mainSource).toContain('<RuntimeTemplates draft={draft} onUpdate={patchRuntime} />');
    expect(mainSource).toContain('auxSection === "diagnostics"');
    expect(mainSource).toContain('<Diagnostics draft={draft} validation={validation}');
    expect(source).not.toContain('className="wsw-aux-overlay"');
    expect(source).not.toContain('className="wsw-aux-panel"');
  });

  it("uses a dedicated spacious toggle for each direction Worker enable control", () => {
    const source = readFileSync(join(process.cwd(), "components", "WorkerSettingsWorkspace.tsx"), "utf8");
    expect(source).toContain('wsw-worker-toggle');
    expect(css).toContain('.wsw-worker-toggle');
    expect(css).toContain('.wsw-worker-toggle .wsw-switch-pill');
    expect(css).toContain('.wsw-worker-toggle b');
    expect(css).toContain('min-width: 132px;');
  });

  it("labels the system Worker policy switch with clear on/off status states", () => {
    expect(css).toContain(".wsw-policy-toggle.is-on");
    expect(css).toContain(".wsw-policy-toggle.is-off");
    expect(css).toContain(".wsw-policy-led");
    expect(css).toContain(".wsw-policy-copy");
  });

  it("spaces the ReasonSwarm strategy cards as independent glass surfaces", () => {
    const rule = ruleContaining(".wsw-reason-grid {", "repeat(2, minmax(340px, 1fr))");
    expect(rule).toContain("grid-template-columns: minmax(360px, .95fr) repeat(2, minmax(340px, 1fr));");
    expect(rule).toContain("gap: clamp(20px, 2vw, 30px);");
    expect(rule).toContain("align-items: start;");
    expect(css).toContain(".wsw-reason-grid > .wsw-runtime-card");
    expect(css).toContain(".wsw-reason-grid .wsw-card-head");
    expect(css).toContain(".wsw-reason-grid .wsw-runtime-fields");
    expect(css).toContain("@media (max-width: 1500px) and (min-width: 981px)");
    expect(css).toContain(".wsw-review-card { grid-column: 1 / -1; }");
  });

  it("uses a compact status-led control for the ReasonSwarm system Worker policy", () => {
    const source = readFileSync(join(process.cwd(), "components", "WorkerSettingsWorkspace.tsx"), "utf8");
    const rule = lastRule(".wsw-policy-toggle {");
    expect(source).toContain("wsw-policy-led");
    expect(source).toContain("wsw-policy-copy");
    expect(css).toContain(".wsw-policy-led");
    expect(css).toContain(".wsw-policy-copy");
    expect(rule).toContain("display: inline-flex;");
    expect(rule).toContain("min-width: 118px;");
    expect(rule).toContain("padding: 8px 11px;");
    expect(css).toContain(".wsw-policy-toggle > .wsw-policy-led");
    expect(css).toContain(".wsw-policy-toggle > .wsw-policy-copy");
    expect(css).toContain("appearance: none;");
  });

  it("shows Planner and Titler provider details clearly and chooses provider models from a select", () => {
    const source = readFileSync(join(process.cwd(), "components", "WorkerSettingsWorkspace.tsx"), "utf8");
    const reasonSource = source.slice(source.indexOf('const profile = draft.llm_profiles[key];'), source.indexOf('<datalist id="wsw-llm-models">'));
    expect(reasonSource).toContain('className="wsw-provider-facts"');
    expect(reasonSource).toContain('modelOptions.length ? <select className="wsw-model-select"');
    expect(reasonSource).not.toContain('className="wsw-model-input" list={`wsw-llm-models-${key}`}');
    expect(reasonSource).not.toContain('w("providerModels")');
    expect(css).toContain(".wsw-provider-facts");
    expect(css).toContain("grid-template-columns: repeat(2, minmax(150px, 1fr));");
  });
});



describe("startup test diagnostics layout", () => {
  const source = readFileSync(join(process.cwd(), "components", "StartupTestPanel.tsx"), "utf8");

  it("uses a centered spacious diagnostics surface instead of a cramped right drawer", () => {
    expect(source).toContain('startup-test-backdrop');
    expect(source).toContain('startup-test-drawer');
    expect(css).toContain('.startup-test-backdrop');
    expect(css).toContain('justify-content: center;');
    expect(css).toContain('align-items: flex-start;');
    expect(css).toContain('width: min(1180px, calc(100vw - 48px));');
    expect(source).toContain('startup-test-close');
    expect(css).toContain('height: min(820px, calc(100dvh - 32px));');
    expect(css).toContain('max-height: calc(100dvh - 32px);');
    expect(css).toContain('border-radius: 24px;');
    expect(css).toContain('flex: 1 1 auto;');
    expect(css).toContain('min-height: 0;');
    expect(css).toContain('overflow-wrap: anywhere;');
    expect(css).toContain('.startup-test-close');
    expect(source).toContain('window.addEventListener("keydown", onKeyDown)');
    expect(source).toContain('startup-test-footer');
    expect(source).toContain('检测蜂群');
    expect(source).toContain('Esc 可关闭');
    expect(source.match(/className="startup-test-close/g)?.length).toBe(1);
    expect(source.match(/className="startup-test-rerun/g)?.length).toBe(1);
    expect(source).not.toContain('关闭面板');
    expect(source).not.toContain('测试配置');
    expect(css).toContain('.startup-test-footer');
    expect(css).toContain('flex: 0 0 auto;');
    expect(css).toContain('overscroll-behavior: contain;');
  });

  it("keeps the startup test panel fixed above the app shell so the bottom is reachable", () => {
    expect(css).toMatch(/\.cc-shell\s*>\s*\.startup-test-backdrop\s*\{[\s\S]*?position:\s*fixed;[\s\S]*?inset:\s*0;/);
    expect(css).toMatch(/\.cc-shell\s*>\s*\.startup-test-backdrop\s*\{[\s\S]*?z-index:\s*60;/);
  });

  it("renders realtime events as a structured status timeline", () => {
    expect(source).toContain('role="list"');
    expect(source).toContain('startup-test-event');
    expect(source).toContain('startup-test-event-dot');
    expect(source).toContain('startupTestEventLabel');
    expect(source).toContain('startupTestEventDetail');
    expect(css).toContain('.startup-test-event');
    expect(css).toContain('.startup-test-event-dot');
    expect(css).toContain('.startup-test-event-top');
    expect(css).toContain('grid-template-columns: 12px minmax(0, 1fr) auto;');
    const eventsRule = lastRule('.startup-test-events {');
    expect(eventsRule).toContain('overflow: visible;');
    expect(eventsRule).not.toContain('overflow: auto;');
  });
});
