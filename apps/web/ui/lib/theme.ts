"use client";

/**
 * Theme state shared by every surface (deck, detail pages, BTW page).
 * Persists to localStorage ("dswarm.theme") and reflects onto
 * <html data-theme> exactly like the deck always did — extracted so the
 * detail routes stop rendering in the wrong theme (operator feedback: the
 * detail pages showed light mode while the deck was dark).
 */

import { useCallback, useEffect, useState } from "react";
import { readKey, writeKey } from "./storage";

export type ThemeMode = "light" | "dark";

export function useTheme(): { theme: ThemeMode; toggleTheme: () => void } {
  const [theme, setTheme] = useState<ThemeMode>("light");

  useEffect(() => {
    try {
      const saved = readKey("dswarm.theme");
      if (saved === "dark" || saved === "light") {
        setTheme(saved);
        return;
      }
      if (window.matchMedia?.("(prefers-color-scheme: dark)").matches) setTheme("dark");
    } catch {
      // keep the default light theme when storage/media is unavailable
    }
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      writeKey("dswarm.theme", theme);
    } catch {
      // theming should still work for this session
    }
  }, [theme]);

  const toggleTheme = useCallback(
    () => setTheme((cur: ThemeMode) => (cur === "dark" ? "light" : "dark")),
    [],
  );

  return { theme, toggleTheme };
}
