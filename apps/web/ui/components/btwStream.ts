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
