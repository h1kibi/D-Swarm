/**
 * Display prettifier for fact content (UI-only; the raw text always stays one
 * click away in the evidence row's disclosure + copy button).
 *
 * The kernel now records the worker's closing WORDS (extract_closing_prose),
 * but facts recorded before that fix embed the raw pi conversation snapshot:
 *   `[pi] {"type":"agent_end","messages":[{"role":"user",...},{"role":"assistant",...}]}`
 * For display we pull the assistant-authored text out of that envelope; a
 * snapshot with only the harness CONCLUDE/user directive yields the actor
 * prefix alone (the raw JSON stays available in the disclosure).
 */

const ENVELOPE_TYPES = "agent_end|message_end|turn_end|message";
const PREFIXED_ENVELOPE = new RegExp(
  '^\\[([^\\]]+)\\]\\s*(\\{\\s*"type"\\s*:\\s*"(?:' + ENVELOPE_TYPES + '")[\\s\\S]*)$',
);
const BARE_ENVELOPE = new RegExp(
  '^\\{\\s*"type"\\s*:\\s*"(?:' + ENVELOPE_TYPES + '")[\\s\\S]*$',
);

export function prettyFact(raw: string): string {
  const text = String(raw || "");
  const m = text.match(PREFIXED_ENVELOPE);
  if (!m) {
    // a bare truncated envelope (no closing brace -> JSON.parse would fail)
    if (BARE_ENVELOPE.test(text)) return "…";
    return text;
  }
  const actor = m[1];
  let ev: any;
  try {
    ev = JSON.parse(m[2]);
  } catch {
    // truncated snapshot (facts are capped at 200 chars): JSON can never
    // close. The assistant words are unrecoverable — say so instead of
    // dumping the raw JSON.
    return `[${actor}] …`;
  }
  if (!ev || typeof ev !== "object") return text;
  const type = String(ev.type || "");
  if (!new RegExp(`^(${ENVELOPE_TYPES})$`).test(type)) return text;
  const prose = messageProse(ev);
  return prose ? `[${actor}] ${prose}` : `[${actor}]`;
}

function messageProse(ev: any): string {
  const msgs = Array.isArray(ev.messages) ? ev.messages : [];
  const single = ev.message && typeof ev.message === "object" ? [ev.message] : [];
  let found = "";
  // bottom-up, assistant-authored only (harness user directives are boilerplate)
  for (const msg of [...msgs, ...single].reverse()) {
    if (!msg || typeof msg !== "object") continue;
    if (String(msg.role || "").toLowerCase() !== "assistant") continue;
    const content = msg.content;
    if (typeof content === "string" && content.trim()) {
      found = content.trim();
    } else if (Array.isArray(content)) {
      const texts = content
        .map((c: any) => (c && typeof c === "object" ? c.text : c))
        .filter((v: any) => typeof v === "string" && v.trim());
      if (texts.length) found = String(texts[texts.length - 1]).trim();
    } else if (typeof msg.text === "string" && msg.text.trim()) {
      found = msg.text.trim();
    }
    if (found) break;
  }
  return found;
}


/** True when a fact carries no worker-authored words (a bare actor prefix,
 *  a truncated-snapshot ellipsis / empty extraction): candidates for
 *  display-suppression in dense views. */
export function isLowInfoFact(raw: string): boolean {
  const pretty = prettyFact(raw);
  const stripped = pretty
    .replace(/^\[[^\]]*\]\s*/, "")
    .replace(/…/g, "")
    .trim();
  return stripped.length === 0;
}
