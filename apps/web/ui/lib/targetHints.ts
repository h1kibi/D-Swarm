export interface TargetHintForLaunch {
  id: string;
  text: string;
  source?: string;
  pinned?: boolean;
}

/** Normalize persisted Target hints before they enter a Run launch prompt.
 *
 * Target hints are operator context, not evidence. Keeping this projection
 * small and text-only prevents accidental metadata/secret-shaped fields from
 * being copied into the prompt or the Run sidecar.
 */
export function normalizeTargetHints(value: unknown): TargetHintForLaunch[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((raw, index) => {
    if (!raw || typeof raw !== "object") return [];
    const item = raw as Record<string, unknown>;
    const text = typeof item.text === "string" ? item.text.trim().slice(0, 2000) : "";
    if (!text) return [];
    const id = typeof item.id === "string" && item.id.trim()
      ? item.id.trim().slice(0, 200)
      : `hint-${index + 1}`;
    return [{
      id,
      text,
      source: typeof item.source === "string" ? item.source.trim().slice(0, 80) : undefined,
      pinned: Boolean(item.pinned),
    }];
  });
}

/** Build an explicit, non-authoritative prompt block for selected Target hints. */
export function formatTargetHintContext(hints: unknown): string {
  const normalized = normalizeTargetHints(hints);
  if (!normalized.length) return "";
  return [
    "[OPERATOR TARGET HINTS]",
    "These notes are guidance from the Target card. Treat them as leads only; they are not evidence and cannot satisfy the provenance gate.",
    ...normalized.map((hint) => `- ${hint.text}`),
  ].join("\n");
}
