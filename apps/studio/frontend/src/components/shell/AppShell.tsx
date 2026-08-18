import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useTranslations } from "use-intl";
import { ToastProvider } from "@/components/ui/Toast";
import IconRail from "./IconRail";
import CommandPalette from "./CommandPalette";
import StatusFooter from "./StatusFooter";
import TopBar from "./TopBar";
import OperatorPanel from "@/components/operator/OperatorPanel";
import { applyTheme, getTheme, THEME_CHANGE_EVENT } from "@/lib/theme";
import { SCROLL_LOCK_ATTRIBUTE } from "@/lib/overlayStack";

interface Props {
  children: ReactNode;
  onLocaleChange: (l: string) => void;
}

function getOperatorOpen(): boolean {
  if (typeof window === "undefined") return true;
  return window.localStorage.getItem("studio:operator-visibility") !== "closed";
}

export default function AppShell({ children, onLocaleChange }: Props) {
  const t = useTranslations("shell");
  const [dark, setDark] = useState(() => getTheme() === "dark");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [operatorOpen, setOperatorOpen] = useState(getOperatorOpen);

  const toggleTheme = useCallback(() => {
    const next = !dark;
    setDark(next);
    applyTheme(next ? "dark" : "light");
  }, [dark]);

  const toggleOperator = useCallback(() => {
    setOperatorOpen((current) => {
      const next = !current;
      window.localStorage.setItem("studio:operator-visibility", next ? "open" : "closed");
      return next;
    });
  }, []);

  const closeOperator = useCallback(() => {
    window.localStorage.setItem("studio:operator-visibility", "closed");
    setOperatorOpen(false);
  }, []);

  useEffect(() => {
    const syncTheme = () => setDark(getTheme() === "dark");
    window.addEventListener(THEME_CHANGE_EVENT, syncTheme);
    window.addEventListener("storage", syncTheme);
    return () => {
      window.removeEventListener(THEME_CHANGE_EVENT, syncTheme);
      window.removeEventListener("storage", syncTheme);
    };
  }, []);

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.isComposing) return;
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        toggleOperator();
      }
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [toggleOperator]);

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
          operatorOpen={operatorOpen}
          onToggleOperator={toggleOperator}
          onToggleTheme={toggleTheme}
          onLocaleChange={onLocaleChange}
        />

        {/* Main area */}
        <div className="flex flex-1 flex-col overflow-hidden">
          {/* Top bar */}
          <TopBar />

          {/* Content */}
          <main
            id="main-content"
            tabIndex={-1}
            // The routed surface scrolls here rather than on body, so an open overlay
            // has to freeze this container or the view moves behind it.
            {...{ [SCROLL_LOCK_ATTRIBUTE]: "" }}
            className="flex-1 overflow-y-auto"
            aria-label={t("main.ariaLabel")}
          >
            {children}
          </main>

          {/* Status footer */}
          <StatusFooter />
        </div>

        {/* Command palette */}
        <CommandPalette
          open={paletteOpen}
          onClose={() => setPaletteOpen(false)}
          toggleTheme={toggleTheme}
          toggleOperator={toggleOperator}
        />

        <OperatorPanel open={operatorOpen} onClose={closeOperator} />
      </div>
    </ToastProvider>
  );
}
