"use client";

/**
 * Cross-view linking (docs/07 Phase 5): Fact ↔ Worker ↔ Intent ↔ source event
 * jumps. Anchors + a transient highlight only — no routing. The Decision
 * Timeline tags every row with `tl-${item.id}` (see DecisionTimeline.tsx), so
 * any view that knows a timeline item id (fact seq / intent id) can scroll to
 * and flash it. No-op when the target is not mounted (e.g. legacy item kinds).
 */

/** DOM id of a Decision Timeline row for a timeline item id (`fact:12`, …). */
export function timelineItemDomId(itemId: string): string {
  return `tl-${itemId}`;
}

/** Scroll the timeline row into view and pulse it. Returns false when the
 *  row is not in the DOM (nothing to jump to). */
export function jumpToTimelineItem(itemId: string): boolean {
  if (typeof document === "undefined") return false;
  const el = document.getElementById(timelineItemDomId(itemId));
  if (!el) return false;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.add("tl-flash");
  window.setTimeout(() => el.classList.remove("tl-flash"), 1600);
  return true;
}

/** Jump to the timeline FACT row for a shared_graph fact seq (source event). */
export function jumpToFactEvent(factSeq: number): boolean {
  return jumpToTimelineItem(`fact:${factSeq}`);
}

/** Jump to the timeline DISPATCH row for an intent id. */
export function jumpToIntentDispatch(intentId: string): boolean {
  return jumpToTimelineItem(`dispatch:${intentId}`);
}
