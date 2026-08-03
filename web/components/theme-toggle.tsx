"use client";

import { Moon, Sun } from "lucide-react";

/**
 * Light/dark toggle.
 *
 * Holds no React state on purpose. The theme lives in one place -- the `dark`
 * class on <html>, set before paint by the inline script in layout.tsx -- and
 * the two icons are swapped by CSS rather than by a `useState` the server would
 * have to guess. A stateful version renders one icon on the server and possibly
 * the other on the client, which is a hydration mismatch on the first paint of
 * every page.
 *
 * ponytail: no system-preference listener and no cross-tab sync. Once a choice
 * is stored, changing the OS theme mid-session will not follow until reload.
 * Add a `matchMedia` change listener if that turns out to matter.
 */
export function ThemeToggle() {
  return (
    <button
      type="button"
      aria-label="Toggle dark mode"
      data-testid="theme-toggle"
      className="hover:bg-accent hover:text-accent-foreground inline-flex size-8 items-center justify-center rounded-md transition-colors"
      onClick={() => {
        const isDark = document.documentElement.classList.toggle("dark");
        try {
          localStorage.setItem("theme", isDark ? "dark" : "light");
        } catch {
          // Private mode or blocked storage: the toggle still works for this
          // page, it just will not be remembered. Not worth failing over.
        }
      }}
    >
      <Sun className="size-4 dark:hidden" />
      <Moon className="hidden size-4 dark:block" />
    </button>
  );
}
