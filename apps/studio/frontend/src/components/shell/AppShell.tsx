import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useTranslations } from "use-intl";
import { ToastProvider } from "@/components/ui/Toast";
import LeoPanel from "@/components/leo/LeoPanel";
import IconRail from "./IconRail";
import CommandPalette from "./CommandPalette";
import StatusFooter from "./StatusFooter";
import TopBar from "./TopBar";

const LEO_OPEN_KEY = "studio:leo-open";

interface Props {
  children: ReactNode;
  locale: string;
  onLocaleChange: (l: string) => void;
}

function getTheme(): "dark" | "light" {
  if (typeof document === "undefined") return "dark";
  return (document.documentElement.getAttribute("data-theme") as "dark" | "light") ?? "dark";
}

function applyTheme(theme: "dark" | "light") {
  document.documentElement.setAttribute("data-theme", theme);
  if (theme === "dark") {
    document.documentElement.classList.add("dark");
  } else {
    document.documentElement.classList.remove("dark");
  }
  localStorage.setItem("theme", theme);
}

export default function AppShell({ children, locale, onLocaleChange }: Props) {
  const t = useTranslations("shell");
  const [dark, setDark] = useState(() => getTheme() === "dark");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [leoOpen, setLeoOpen] = useState(
    () => typeof window !== "undefined" && localStorage.getItem(LEO_OPEN_KEY) === "1",
  );

  const setLeo = useCallback((open: boolean) => {
    setLeoOpen(open);
    localStorage.setItem(LEO_OPEN_KEY, open ? "1" : "0");
  }, []);

  const toggleLeo = useCallback(() => {
    setLeoOpen((v) => {
      const next = !v;
      localStorage.setItem(LEO_OPEN_KEY, next ? "1" : "0");
      return next;
    });
  }, []);

  const toggleTheme = useCallback(() => {
    const next = !dark;
    setDark(next);
    applyTheme(next ? "dark" : "light");
  }, [dark]);

  // Binary en/zh flip kept for the command palette's "Switch language" action;
  // the rail's own selector calls onLocaleChange directly with any of the 16 codes.
  const toggleLocale = useCallback(() => {
    onLocaleChange(locale === "en" ? "zh" : "en");
  }, [locale, onLocaleChange]);

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.isComposing) return;
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "j") {
        e.preventDefault();
        toggleLeo();
      }
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [toggleLeo]);

  const isTauri = typeof window !== "undefined" && "__TAURI__" in window;

  return (
    <ToastProvider>
      <div className="flex h-dvh overflow-hidden bg-surface-base font-ui text-content-primary">
        {/* Tauri top drag region */}
        {isTauri && (
          <div
            data-tauri-drag-region
            aria-hidden="true"
            className="fixed left-0 right-0 top-0 z-50 h-10"
          />
        )}

        {/* Icon rail */}
        <IconRail
          dark={dark}
          onToggleTheme={toggleTheme}
          onLocaleChange={onLocaleChange}
          leoOpen={leoOpen}
          onToggleLeo={toggleLeo}
        />

        {/* Main area */}
        <div className="flex flex-1 flex-col overflow-hidden">
          {/* Top bar */}
          <TopBar />

          {/* Content */}
          <main
            id="main-content"
            tabIndex={-1}
            className="flex-1 overflow-y-auto"
            aria-label={t("main.ariaLabel")}
          >
            {children}
          </main>

          {/* Status footer */}
          <StatusFooter />
        </div>

        {/* Leo — persistent side panel: zero-width when minimized (the rail
            chat icon + ⌘J reopen it), never unmounts so the session survives */}
        <LeoPanel expanded={leoOpen} onMinimize={() => setLeo(false)} />

        {/* Command palette */}
        <CommandPalette
          open={paletteOpen}
          onClose={() => setPaletteOpen(false)}
          toggleTheme={toggleTheme}
          toggleLocale={toggleLocale}
        />
      </div>
    </ToastProvider>
  );
}
