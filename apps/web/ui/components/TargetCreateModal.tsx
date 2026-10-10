"use client";

import { useEffect, useState } from "react";
import { Icon } from "@/components/Icon";

export interface TargetCreateForm {
  name: string;
  kind: "ctf" | "pentest";
  category: "web" | "pwn" | "crypto" | "reverse" | "misc";
  description: string;
  target_address: string;
  flag_format_hint: string;
  expected_flags: number;
  multi_flag: boolean;
  direction: string;
  direction_description: string;
}

const INITIAL_FORM: TargetCreateForm = {
  name: "",
  kind: "ctf",
  category: "web",
  description: "",
  target_address: "",
  flag_format_hint: "",
  expected_flags: 1,
  multi_flag: false,
  direction: "",
  direction_description: "",
};
const KIND_OPTIONS: Array<[TargetCreateForm["kind"], string]> = [["ctf", "CTF 题目"], ["pentest", "渗透测试"]];
const CATEGORY_OPTIONS: Array<[TargetCreateForm["category"], string]> = [["web", "Web"], ["pwn", "Pwn"], ["crypto", "Crypto"], ["reverse", "Reverse"], ["misc", "其他"]];

export function TargetCreateModal({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (form: TargetCreateForm) => void | Promise<void>;
}) {
  const [form, setForm] = useState<TargetCreateForm>(INITIAL_FORM);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, saving]);

  const update = <K extends keyof TargetCreateForm>(key: K, value: TargetCreateForm[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    if (error) setError("");
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const name = form.name.trim();
    const description = form.description.trim();
    if (!name) {
      setError("请输入 Target 名称。");
      return;
    }
    if (!description) {
      setError("请输入题目描述，后续 Run 会默认读取它。");
      return;
    }
    setSaving(true);
    try {
      await onCreate({
        ...form,
        name,
        description,
        target_address: form.target_address.trim(),
        flag_format_hint: form.flag_format_hint.trim(),
        direction: form.direction.trim(),
        direction_description: form.direction_description.trim(),
        expected_flags: form.multi_flag ? Math.max(1, form.expected_flags || 1) : 1,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Target 创建失败，请重试。");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="target-create-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !saving) onClose(); }}>
      <section className="target-create-modal" role="dialog" aria-modal="true" aria-labelledby="target-create-title">
        <header className="target-create-header">
          <div className="target-create-heading">
            <span className="target-create-kicker"><Icon name="target" size={13} /> 新建目标</span>
            <h2 id="target-create-title">建立一个调查目标</h2>
            <p>先保存目标和题目背景，之后再为它创建独立 Run。</p>
          </div>
          <button className="target-details-close" type="button" onClick={onClose} disabled={saving} aria-label="关闭"><Icon name="x" size={17} /></button>
        </header>
        <form onSubmit={submit}>
          <div className="target-create-body">
            <section className="target-create-primary-block">
              <div className="target-create-section-head"><div><strong>基本信息</strong><span>这是工作台中最常用的目标识别信息。</span></div><b>01</b></div>
              <label className="target-create-field target-create-field-hero"><span>目标名称 *</span><input autoFocus value={form.name} onChange={(event) => update("name", event.target.value)} placeholder="例如：登录门户、API 网关、逆向样本" /></label>
              <label className="target-create-field target-create-field-wide"><span>目标地址 / 主机</span><input value={form.target_address} onChange={(event) => update("target_address", event.target.value)} placeholder="https://example.test 或主机:端口" /></label>
              <label className="target-create-field"><span>题目描述 *</span><textarea value={form.description} onChange={(event) => update("description", event.target.value)} rows={4} placeholder="写下题目背景、目标、已知条件或需要验证的问题。" /></label>
              <div className="target-create-choice-row">
                <div className="target-create-field"><span>工作模式</span><div className="target-create-segmented" role="radiogroup" aria-label="工作模式">{KIND_OPTIONS.map(([value, label]) => <button key={value} type="button" className={form.kind === value ? "active" : ""} onClick={() => update("kind", value)} aria-pressed={form.kind === value}>{label}</button>)}</div></div>
                <div className="target-create-field"><span>目标类型</span><div className="target-create-segmented target-create-categories" role="radiogroup" aria-label="目标类型">{CATEGORY_OPTIONS.map(([value, label]) => <button key={value} type="button" className={form.category === value ? "active" : ""} onClick={() => update("category", value)} aria-pressed={form.category === value}>{label}</button>)}</div></div>
              </div>
            </section>
            <button className={`target-create-advanced-toggle ${advancedOpen ? "open" : ""}`} type="button" onClick={() => setAdvancedOpen((value) => !value)} aria-expanded={advancedOpen}>
              <span><Icon name={advancedOpen ? "chevronDown" : "chevronRight"} size={14} /><b>高级设置</b><small>Flag 格式、方向与多 Flag 规则</small></span>
            </button>
            {advancedOpen && <div className="target-create-advanced">
              <div className="target-create-choice-row">
                <label className="target-create-field"><span>Flag 格式</span><input value={form.flag_format_hint} onChange={(event) => update("flag_format_hint", event.target.value)} placeholder="例如：flag{...}" /></label>
              </div>
              <div className="target-create-options">
                <label className="target-create-check"><input type="checkbox" checked={form.multi_flag} onChange={(event) => update("multi_flag", event.target.checked)} /><span><b>收集多个 Flag</b><small>只在题目明确要求多个结果时开启。</small></span></label>
                {form.multi_flag && <label className="target-create-count"><span>目标数量</span><input type="number" min={1} max={100} value={form.expected_flags} onChange={(event) => update("expected_flags", Math.max(1, Number(event.target.value) || 1))} /></label>}
              </div>
              <div className="target-create-direction">
                <div className="target-create-section-head"><div><strong>初始主方向</strong><span>可选。后续 Reason 仍可规划其他路线。</span></div><b>可选</b></div>
                <div className="target-create-choice-row"><label className="target-create-field"><span>方向名称</span><input value={form.direction} onChange={(event) => update("direction", event.target.value)} placeholder="例如：登录与权限边界" /></label><label className="target-create-field"><span>方向说明</span><input value={form.direction_description} onChange={(event) => update("direction_description", event.target.value)} placeholder="优先验证什么？" /></label></div>
              </div>
            </div>}
            {error && <p className="target-create-error" role="alert">{error}</p>}
          </div>
          <footer className="target-create-footer"><span className="target-create-hint">创建后可在目标详情中继续补充信息。</span><button className="target-ghost-button" type="button" onClick={onClose} disabled={saving}>取消</button><button className="target-primary-button" type="submit" disabled={saving}>{saving ? "正在创建…" : "创建目标"}<Icon name="chevronRight" size={13} /></button></footer>
        </form>
      </section>
    </div>
  );
}
