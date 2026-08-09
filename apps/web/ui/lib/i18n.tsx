"use client";

import { createContext, useContext, useEffect, useState } from "react";

/**
 * Lightweight i18n for the command deck — static UI strings only (the agent's
 * streaming reasoning / insights / tool output are passed through verbatim, in
 * whatever language the swarm produced). Default is Chinese (zh). The chosen
 * language is persisted to localStorage and exposed via a tiny context + hook.
 *
 * No dependency: a flat key → { zh, en } table and a `t(key, vars?)` lookup.
 * `{name}`-style placeholders are interpolated.
 */

export type Lang = "zh" | "en";

type Dict = Record<string, { zh: string; en: string }>;

import { STRINGS } from "./strings";
import { readKey, writeKey } from "./storage";

const LangCtx = createContext<{ lang: Lang; setLang: (l: Lang) => void }>({
  lang: "zh",
  setLang: () => {},
});

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLangState] = useState<Lang>("zh");
  // hydrate from localStorage after mount (avoids SSR mismatch)
  useEffect(() => {
    const saved = readKey("dswarm.lang") as Lang | null;
    if (saved === "zh" || saved === "en") setLangState(saved);
  }, []);
  const setLang = (l: Lang) => {
    setLangState(l);
    writeKey("dswarm.lang", l);
  };
  return <LangCtx.Provider value={{ lang, setLang }}>{children}</LangCtx.Provider>;
}

export function useLang() {
  return useContext(LangCtx);
}

/** Translate a key with optional `{var}` interpolation. */
export function useT() {
  const { lang } = useContext(LangCtx);
  return (key: string, vars?: Record<string, string | number>): string => {
    const entry = STRINGS[key];
    let s = entry ? entry[lang] : key;
    if (vars) for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, String(v));
    return s;
  };
}
