import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useLocale, useTranslations } from "use-intl";
import { buildRegistry, fuzzyMatch, type Command } from "@/lib/commands";

interface Props {
  open: boolean;
  onClose: () => void;
  toggleTheme: () => void;
  toggleLocale: () => void;
}

/** Inner palette — mounted with a fresh key each time `open` goes true. */
function PaletteInner({ onClose, toggleTheme, toggleLocale }: Omit<Props, "open">) {
  const t = useTranslations("shell");
  const locale = useLocale();
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const listboxRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);

  // Filtered commands — computed from current query
  const doNavigate = useCallback(
    (href: string) => {
      // Space-tab hrefs carry search params ("/library?tab=agent")
      const [path, qs] = href.split("?");
      const search = qs ? Object.fromEntries(new URLSearchParams(qs)) : undefined;
      void navigate({ to: path, search });
      onClose();
    },
    [navigate, onClose],
  );

  const commands: Command[] = buildRegistry(doNavigate, toggleTheme, toggleLocale);
  const filtered = commands.filter((c) => fuzzyMatch(query, c));

  const execute = useCallback(
    (cmd: Command) => {
      if (cmd.href) {
        doNavigate(cmd.href);
      } else if (cmd.action) {
        cmd.action();
        onClose();
      }
    },
    [doNavigate, onClose],
  );

  // Focus input on mount
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Scroll active option into view
  useEffect(() => {
    const el = listboxRef.current?.querySelectorAll<HTMLElement>("[role=option]")[active];
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);

  // Global keyboard handler for navigation within the palette
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      // Never react while an IME composition is in flight (zh input):
      // Enter/arrows there confirm the composition, not a command.
      if (e.isComposing) return;
      const vimNav = e.ctrlKey && !e.metaKey && !e.altKey;
      if (e.key === "Escape") {
        onClose();
      } else if (e.key === "ArrowDown" || (vimNav && e.key === "j")) {
        e.preventDefault();
        setActive((a) => Math.min(a + 1, filtered.length - 1));
      } else if (e.key === "ArrowUp" || (vimNav && e.key === "k")) {
        e.preventDefault();
        setActive((a) => Math.max(a - 1, 0));
      } else if (e.key === "Enter") {
        const cmd = filtered[active];
        if (cmd) execute(cmd);
      }
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose, filtered, active, execute]);

  function handleQueryChange(value: string) {
    setQuery(value);
    setActive(0);
  }

  const sections = Array.from(new Set(filtered.map((c) => c.section ?? ""))).filter(Boolean);
  const activeId = filtered[active] ? `cmd-opt-${filtered[active].id}` : undefined;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[15vh]"
      aria-modal="true"
      role="dialog"
      aria-label={t("palette.ariaLabel")}
    >
      {/* Scrim button */}
      <button
        type="button"
        aria-label="Close command palette"
        className="absolute inset-0 bg-black/50 cursor-default"
        onClick={onClose}
        tabIndex={-1}
      />

      {/* Panel */}
      <div
        className="relative w-full max-w-lg overflow-hidden rounded-lg border border-edge-strong bg-surface-overlay"
        style={{ boxShadow: "0 24px 48px rgba(0,0,0,0.6)" }}
      >
        {/* Input */}
        <div className="flex items-center gap-2 border-b border-edge px-4 py-3">
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
            className="shrink-0 text-content-muted"
          >
            <circle cx="11" cy="11" r="8" />
            <path d="m21 21-4.35-4.35" />
          </svg>
          <input
            ref={inputRef}
            role="combobox"
            aria-expanded={filtered.length > 0}
            aria-controls="cmd-listbox"
            aria-activedescendant={activeId}
            aria-autocomplete="list"
            type="text"
            value={query}
            onChange={(e) => handleQueryChange(e.target.value)}
            placeholder={t("palette.placeholder")}
            aria-label={t("palette.placeholder")}
            className="flex-1 bg-transparent font-ui text-[length:var(--t-base)] text-content-primary outline-none placeholder:text-content-muted"
          />
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded border border-edge bg-surface-raised px-1.5 py-0.5 font-data text-[length:var(--t-xs)] text-content-muted transition-colors"
          >
            ESC
          </button>
        </div>

        {/* Results */}
        <div
          id="cmd-listbox"
          ref={listboxRef}
          role="listbox"
          aria-label={t("palette.resultsLabel")}
          className="max-h-80 overflow-y-auto py-1"
        >
          {filtered.length === 0 && (
            <div className="px-4 py-6 text-center text-[length:var(--t-sm)] text-content-muted">
              {t("palette.empty")}
            </div>
          )}

          {sections.map((section) => {
            const sectionItems = filtered.filter((c) => (c.section ?? "") === section);
            return (
              <div key={section} role="group" aria-label={section}>
                <div className="px-4 py-1 text-[length:var(--t-xs)] uppercase tracking-[0.1em] text-content-muted">
                  {section}
                </div>
                {sectionItems.map((cmd) => {
                  const idx = filtered.indexOf(cmd);
                  const isActive = idx === active;
                  const label = locale === "zh" && cmd.labelZh ? cmd.labelZh : cmd.label;
                  return (
                    <div
                      key={cmd.id}
                      id={`cmd-opt-${cmd.id}`}
                      role="option"
                      aria-selected={isActive}
                      tabIndex={-1}
                      onClick={() => execute(cmd)}
                      onMouseEnter={() => setActive(idx)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") execute(cmd);
                      }}
                      className={`flex cursor-pointer items-center gap-3 px-4 py-2 text-[length:var(--t-base)] transition-colors ${
                        isActive
                          ? "bg-surface-raised text-content-primary"
                          : "text-content-secondary"
                      }`}
                    >
                      <span className="flex-1 truncate">{label}</span>
                      {cmd.href && (
                        <span className="font-data text-[length:var(--t-xs)] text-content-muted">
                          ↵
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/** Outer wrapper — mounts PaletteInner with a fresh key each time `open` toggles true. */
export default function CommandPalette({ open, onClose, toggleTheme, toggleLocale }: Props) {
  const [epoch, setEpoch] = useState(0);

  const prevOpenRef = useRef(open);
  useEffect(() => {
    if (!prevOpenRef.current && open) {
      setEpoch((e) => e + 1);
    }
    prevOpenRef.current = open;
  }, [open]);

  if (!open) return null;

  return (
    <PaletteInner
      key={epoch}
      onClose={onClose}
      toggleTheme={toggleTheme}
      toggleLocale={toggleLocale}
    />
  );
}
