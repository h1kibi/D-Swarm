"use client";

import { useEffect, useState } from "react";
import type { DeckState } from "./events";
import { pheromoneClockSec } from "./pheromone";

/**
 * The clock the pheromone UI renders against (docs/07 §6.3, Phase 5).
 *
 * Live runs decay in real time — a low-frequency interval (5s, never faster)
 * re-renders the strength bars. The interval only runs while there ARE
 * findings and the run is still live; a finished/replayed run freezes at its
 * finishedAt (replay virtual time) and needs no timer at all.
 */
export function usePheromoneClock(deck: DeckState, intervalMs = 5000): number {
  const [nowSec, setNowSec] = useState(() => Date.now() / 1000);
  const live = deck.findings.length > 0 && !deck.finished;
  useEffect(() => {
    if (!live) return;
    const id = setInterval(() => setNowSec(Date.now() / 1000), Math.max(5000, intervalMs));
    return () => clearInterval(id);
  }, [live, intervalMs]);
  return pheromoneClockSec(deck, nowSec);
}
