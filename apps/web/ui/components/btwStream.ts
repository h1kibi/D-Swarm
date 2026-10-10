export interface BtwAnswerState {
  content: string;
  final: boolean;
  error: string;
}

export function applyBtwAnswerFrame(
  state: BtwAnswerState,
  frame: Record<string, unknown>,
): BtwAnswerState {
  const next = { ...state };
  if (typeof frame.final === "string") {
    next.content = frame.final;
    next.final = true;
  } else if (!next.final && typeof frame.delta === "string") {
    next.content += frame.delta;
  }
  if (frame.error) {
    next.error = String(frame.error);
    if (!next.final && !next.content) next.content = next.error;
  }
  return next;
}


export function btwErrorBannerText(content: string, error: string): string {
  const answer = (content || "").trim();
  const detail = (error || "").trim();
  return detail && detail !== answer ? detail.slice(0, 300) : "";
}

export type ObserverSource = "default" | "fallback" | "unavailable";

export function observerSourceLabel(source: ObserverSource | undefined): string {
  if (source === "default") return "Observer default";
  if (source === "fallback") return "Observer fallback";
  if (source === "unavailable") return "Observer unavailable";
  return "Observer";
}

export type BtwFailureKind = "http" | "transport" | "empty";

/**
 * The assistant turn for any client-side Observer failure (HTTP non-2xx, stream
 * exception, or an empty stream). Every one of them is attributed to the
 * isolated Observer chain as `unavailable` — never to Titler/Reason, and never
 * as a successful answer (docs/17 §12.2).
 */
export function btwFailureTurn(
  kind: BtwFailureKind,
  detail = "",
): { content: string; observerSource: ObserverSource; fallbackUsed: boolean } {
  const clean = String(detail || "").trim().slice(0, 300);
  const content = kind === "empty"
    ? "观察员未返回任何回答；没有启动深度 worker。"
    : `观察员暂时无法回答：${clean || "未知错误"}`;
  return { content, observerSource: "unavailable", fallbackUsed: false };
}
