import { useCallback, useEffect, useState } from "react";

export type ThemeId = "paper" | "ink" | "system";
export type PersonaId = "professional" | "xiugou";

const THEME_KEY = "legal-helper-theme";
const PERSONA_KEY = "legal-helper-persona";

function prefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches;
}

/** Migrate legacy theme ids (theme-dark → ink, everything else → paper). */
function initialTheme(): ThemeId {
  const raw = typeof localStorage !== "undefined" ? localStorage.getItem(THEME_KEY) : null;
  if (raw === "paper" || raw === "ink" || raw === "system") return raw;
  if (raw && /dark|nocturne|neon|ocean/i.test(raw)) return "ink";
  if (raw) return "paper";
  return "system";
}

function applyTheme(theme: ThemeId) {
  const root = document.documentElement;
  const dark = theme === "ink" || (theme === "system" && prefersDark());
  root.classList.toggle("dark", dark);
  root.dataset.theme = dark ? "dark" : "light";
}

export function useTheme() {
  const [theme, setThemeState] = useState<ThemeId>(initialTheme);
  const [persona, setPersonaState] = useState<PersonaId>(
    () => (localStorage.getItem(PERSONA_KEY) as PersonaId) || "professional",
  );

  useEffect(() => {
    applyTheme(theme);
    localStorage.setItem(THEME_KEY, theme);
    if (theme !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme("system");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [theme]);

  useEffect(() => {
    localStorage.setItem(PERSONA_KEY, persona);
    document.documentElement.dataset.persona = persona;
  }, [persona]);

  const setTheme = useCallback((t: ThemeId) => setThemeState(t), []);
  const setPersona = useCallback((p: PersonaId) => setPersonaState(p), []);
  return { theme, setTheme, persona, setPersona };
}
