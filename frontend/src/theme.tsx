// Display-mode management: light + dark, persisted, applied to <html data-mode>.
//
// `data-mode` is ui-style-kit-css's mode attribute; the library resolves the
// walnut-clay palette for it, and every app token is bridged to those roles in
// kit.css. `data-theme` therefore stays reserved for the library's colour
// scheme (set once in index.html) and is not touched here. "system" tracks the
// OS preference live so the app feels native on a teacher's laptop.

import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from "react";

export type ThemeChoice = "light" | "dark" | "system";

interface ThemeCtx {
  choice: ThemeChoice;
  resolved: "light" | "dark";
  setChoice: (c: ThemeChoice) => void;
  toggle: () => void;
  /** Three-way cycle: light -> dark -> system -> light. */
  cycle: () => void;
}

const Ctx = createContext<ThemeCtx>({
  choice: "system", resolved: "light",
  setChoice: () => {}, toggle: () => {}, cycle: () => {},
});

const STORAGE_KEY = "mc5.theme";

function systemPreferred(): "light" | "dark" {
  if (typeof window === "undefined" || !window.matchMedia) return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(() => {
    if (typeof window === "undefined") return "system";
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
  });
  const [resolved, setResolved] = useState<"light" | "dark">(() =>
    choice === "system" ? systemPreferred() : choice);

  useEffect(() => {
    const apply = () => {
      const next = choice === "system" ? systemPreferred() : choice;
      setResolved(next);
      document.documentElement.setAttribute("data-mode", next);
      document.documentElement.style.colorScheme = next;
    };
    apply();
    if (choice !== "system" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => apply();
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, [choice]);

  const setChoice = useCallback((c: ThemeChoice) => {
    setChoiceState(c);
    try { window.localStorage.setItem(STORAGE_KEY, c); } catch { /* private mode */ }
  }, []);

  const toggle = useCallback(() => {
    setChoice(resolved === "dark" ? "light" : "dark");
  }, [resolved, setChoice]);

  const cycle = useCallback(() => {
    setChoice(choice === "light" ? "dark" : choice === "dark" ? "system" : "light");
  }, [choice, setChoice]);

  const value = useMemo(
    () => ({ choice, resolved, setChoice, toggle, cycle }),
    [choice, resolved, setChoice, toggle, cycle]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme() {
  return useContext(Ctx);
}
