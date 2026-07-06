import { useCallback, useEffect, useRef, useState, type ReactElement } from "react";
import { Link, useLocation, useNavigate } from "@tanstack/react-router";
import { useLocale, useTranslations } from "use-intl";
import { LOCALES } from "@/i18n/locales";

interface Space {
  id: string;
  href: string;
  labelKey: string;
  icon: ReactElement;
  key: number;
}

function IconTarget() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" />
      <circle cx="12" cy="12" r="6" />
      <circle cx="12" cy="12" r="2" />
    </svg>
  );
}

function IconGraph() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="6" cy="5" r="2.5" />
      <circle cx="18" cy="5" r="2.5" />
      <circle cx="12" cy="19" r="2.5" />
      <path d="M7.2 7.2 L10.8 16.8" />
      <path d="M16.8 7.2 L13.2 16.8" />
      <path d="M8.5 5 L15.5 5" />
    </svg>
  );
}

function IconGrid() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="3" width="7" height="7" />
      <rect x="14" y="3" width="7" height="7" />
      <rect x="3" y="14" width="7" height="7" />
      <rect x="14" y="14" width="7" height="7" />
    </svg>
  );
}

function IconGear() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

function IconSun() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="5" />
      <line x1="12" y1="1" x2="12" y2="3" />
      <line x1="12" y1="21" x2="12" y2="23" />
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
      <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
      <line x1="1" y1="12" x2="3" y2="12" />
      <line x1="21" y1="12" x2="23" y2="12" />
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
      <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
    </svg>
  );
}

function IconMoon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

function IconCalendar() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="5" width="18" height="16" rx="2" />
      <line x1="3" y1="10" x2="21" y2="10" />
      <line x1="8" y1="3" x2="8" y2="7" />
      <line x1="16" y1="3" x2="16" y2="7" />
    </svg>
  );
}

const SPACES: Space[] = [
  { id: "home", href: "/", labelKey: "rail.home", icon: <IconTarget />, key: 1 },
  { id: "designer", href: "/designer", labelKey: "rail.designer", icon: <IconGraph />, key: 2 },
  { id: "library", href: "/library", labelKey: "rail.library", icon: <IconGrid />, key: 3 },
  {
    id: "schedules",
    href: "/schedules",
    labelKey: "rail.schedules",
    icon: <IconCalendar />,
    key: 4,
  },
];

// System is configuration, not an operating space — it lives at the rail
// bottom as a gear, keeping the top row for work surfaces.
const SYSTEM_SPACE: Space = {
  id: "system",
  href: "/system",
  labelKey: "rail.system",
  icon: <IconGear />,
  key: 5,
};

function IconChat() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  );
}

interface Props {
  dark: boolean;
  onToggleTheme: () => void;
  onLocaleChange: (locale: string) => void;
  leoOpen?: boolean;
  onToggleLeo?: () => void;
}

function isActive(href: string, pathname: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export default function IconRail({
  dark,
  onToggleTheme,
  onLocaleChange,
  leoOpen,
  onToggleLeo,
}: Props) {
  const t = useTranslations("shell");
  const tc = useTranslations("leo");
  const locale = useLocale();
  const pathname = useLocation().pathname;
  const navigate = useNavigate();

  const [langOpen, setLangOpen] = useState(false);
  const langMenuRef = useRef<HTMLDivElement>(null);
  const langTriggerRef = useRef<HTMLButtonElement>(null);
  const langOptionRefs = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (!langOpen) return;
    function handlePointerDown(e: PointerEvent) {
      if (langMenuRef.current && !langMenuRef.current.contains(e.target as Node)) {
        setLangOpen(false);
      }
    }
    window.addEventListener("pointerdown", handlePointerDown);
    return () => window.removeEventListener("pointerdown", handlePointerDown);
  }, [langOpen]);

  useEffect(() => {
    if (!langOpen) return;
    const activeIdx = LOCALES.findIndex((l) => l.code === locale);
    langOptionRefs.current[activeIdx >= 0 ? activeIdx : 0]?.focus();
  }, [langOpen, locale]);

  const handleLangKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      setLangOpen(false);
      langTriggerRef.current?.focus();
      return;
    }
    const options = langOptionRefs.current;
    const current = options.findIndex((el) => el === document.activeElement);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      options[Math.min(current + 1, options.length - 1)]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      options[Math.max(current - 1, 0)]?.focus();
    }
  }, []);

  const handleKey = useCallback(
    (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const n = parseInt(e.key, 10);
      if (n >= 1 && n <= 5) {
        e.preventDefault();
        const space = n === 5 ? SYSTEM_SPACE : SPACES[n - 1];
        if (space) void navigate({ to: space.href });
      }
    },
    [navigate],
  );

  useEffect(() => {
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [handleKey]);

  const isTauri = typeof window !== "undefined" && "__TAURI__" in window;

  return (
    <nav
      aria-label={t("rail.ariaLabel")}
      className="flex w-14 flex-col items-center border-r border-edge bg-surface-raised pb-2"
      style={{ height: "100%", paddingTop: isTauri ? 40 : 12 }}
    >
      {/* Spaces */}
      <ul className="flex flex-1 flex-col items-center gap-1">
        {SPACES.map((space) => {
          const active = isActive(space.href, pathname);
          const label = t(space.labelKey as Parameters<typeof t>[0]);
          return (
            <li key={space.id}>
              <Link
                to={space.href}
                aria-label={`${label} (⌘${space.key})`}
                title={`${label} — ⌘${space.key}`}
                onClick={(e) => {
                  // Re-clicking the active space toggles its secondary pane
                  // (e.g. the blueprint rack) instead of re-navigating.
                  if (active) {
                    e.preventDefault();
                    window.dispatchEvent(new CustomEvent("studio:toggle-pane"));
                  }
                }}
                className={`group relative flex h-10 w-10 items-center justify-center rounded transition-colors duration-100 ${
                  active ? "bg-surface-overlay text-content-primary" : "text-content-muted"
                }`}
              >
                {/* Amber left hairline indicator — rail-specific active design, not IconButton */}
                {active && (
                  <span
                    aria-hidden="true"
                    className="absolute bottom-2 left-0 top-2 w-0.5 rounded-r bg-accent"
                  />
                )}
                <span
                  className={`transition-opacity duration-100 ${
                    active ? "opacity-100" : "opacity-[0.55] group-hover:opacity-100"
                  }`}
                >
                  {space.icon}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>

      {/* Bottom cluster — system + Leo + theme + locale */}
      <div className="flex flex-col items-center gap-1 pb-2">
        {(() => {
          const active = isActive(SYSTEM_SPACE.href, pathname);
          const label = t(SYSTEM_SPACE.labelKey as Parameters<typeof t>[0]);
          return (
            <Link
              to={SYSTEM_SPACE.href}
              aria-label={`${label} (⌘${SYSTEM_SPACE.key})`}
              title={`${label} — ⌘${SYSTEM_SPACE.key}`}
              className={`group relative flex h-10 w-10 items-center justify-center rounded transition-colors duration-100 ${
                active ? "bg-surface-overlay text-content-primary" : "text-content-muted"
              }`}
            >
              {active && (
                <span
                  aria-hidden="true"
                  className="absolute bottom-2 left-0 top-2 w-0.5 rounded-r bg-accent"
                />
              )}
              <span
                className={`transition-opacity duration-100 ${
                  active ? "opacity-100" : "opacity-[0.55] group-hover:opacity-100"
                }`}
              >
                {SYSTEM_SPACE.icon}
              </span>
            </Link>
          );
        })()}
        {onToggleLeo && (
          <button
            type="button"
            onClick={onToggleLeo}
            aria-label={`${tc("name")} (⌘J)`}
            aria-pressed={leoOpen ?? false}
            className={`group relative flex h-10 w-10 items-center justify-center rounded transition-colors duration-100 ${
              leoOpen ? "bg-surface-overlay text-content-primary" : "text-content-muted"
            }`}
            title={`${tc("name")} — ⌘J`}
          >
            {leoOpen && (
              <span
                aria-hidden="true"
                className="absolute bottom-2 left-0 top-2 w-0.5 rounded-r bg-accent"
              />
            )}
            <span
              className={`transition-opacity duration-100 ${
                leoOpen ? "opacity-100" : "opacity-[0.55] group-hover:opacity-100"
              }`}
            >
              <IconChat />
            </span>
          </button>
        )}

        <button
          type="button"
          onClick={onToggleTheme}
          aria-label={t("rail.toggleTheme")}
          aria-pressed={dark}
          className="flex h-8 w-8 items-center justify-center rounded text-content-muted transition-colors duration-100"
          title={t("rail.toggleTheme")}
        >
          <span className="opacity-[0.55] transition-opacity hover:opacity-100">
            {dark ? <IconSun /> : <IconMoon />}
          </span>
        </button>

        <div ref={langMenuRef} className="relative">
          <button
            ref={langTriggerRef}
            type="button"
            onClick={() => setLangOpen((v) => !v)}
            aria-label={t("rail.selectLanguage")}
            aria-haspopup="listbox"
            aria-expanded={langOpen}
            className="flex h-8 w-8 items-center justify-center rounded font-data text-[length:var(--t-xs)] font-medium text-content-muted transition-colors duration-100"
            title={t("rail.selectLanguage")}
          >
            <span className="opacity-[0.55] transition-opacity hover:opacity-100">
              {locale.slice(0, 2).toUpperCase()}
            </span>
          </button>

          {langOpen && (
            <ul
              role="listbox"
              aria-label={t("rail.selectLanguage")}
              onKeyDown={handleLangKeyDown}
              className="absolute bottom-0 left-full z-50 ml-1 max-h-72 w-44 overflow-y-auto rounded border border-edge bg-surface-overlay py-1 shadow-card"
            >
              {LOCALES.map((l, idx) => {
                const active = l.code === locale;
                return (
                  <li key={l.code} role="option" aria-selected={active}>
                    <button
                      ref={(el) => {
                        langOptionRefs.current[idx] = el;
                      }}
                      type="button"
                      onClick={() => {
                        onLocaleChange(l.code);
                        setLangOpen(false);
                        langTriggerRef.current?.focus();
                      }}
                      dir={l.dir}
                      className={`flex w-full items-center px-3 py-1.5 text-left text-[length:var(--t-sm)] transition-colors duration-100 ${
                        active
                          ? "bg-surface-raised text-content-primary"
                          : "text-content-secondary hover:bg-surface-raised hover:text-content-primary"
                      }`}
                    >
                      {l.native}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </nav>
  );
}
