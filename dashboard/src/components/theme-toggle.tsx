"use client";

import { useEffect, useState } from "react";

type Theme = "light" | "dark";

/**
 * Light/dark toggle.
 *
 * Reads the class the inline script in layout.tsx already applied, so there is
 * no hydration mismatch, and persists the choice. Falls back to the OS
 * preference on first visit.
 */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme | null>(null);

  useEffect(() => {
    const isDark = document.documentElement.classList.contains("dark");
    setTheme(isDark ? "dark" : "light");
  }, []);

  function toggle() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.classList.toggle("dark", next === "dark");
    try {
      localStorage.setItem("gw-theme", next);
    } catch {
      // Storage blocked (private mode): the toggle still works for this page view.
    }
  }

  // Render a stable placeholder until mounted to avoid a hydration mismatch.
  const label = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={label}
      title={label}
      className="grid size-9 place-items-center rounded-lg border border-[var(--border)] bg-[var(--surface)] text-sm transition-colors hover:bg-[var(--surface-muted)]"
    >
      <span aria-hidden="true" className={theme ? "" : "opacity-0"}>
        {theme === "dark" ? "☀️" : "🌙"}
      </span>
    </button>
  );
}