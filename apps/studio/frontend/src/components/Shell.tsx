import { Link, useLocation } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import { Suspense, useEffect, useState, type ReactNode } from "react";
import Breadcrumb from "@/components/nav/Breadcrumb";
import NavGroup from "@/components/nav/NavGroup";
import ProjectChip from "@/components/nav/ProjectChip";
import { NAV_GROUPS } from "@/components/nav/types";

export interface ShellProps {
  children: ReactNode;
}

const RAIL_COLLAPSED_KEY = "studio.railCollapsed";

function readStoredRailCollapsed(): boolean {
  try {
    return window.localStorage.getItem(RAIL_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function BrandMark() {
  return (
    <span
      aria-hidden="true"
      className="flex h-6 w-6 shrink-0 items-center justify-center rounded bg-gradient-to-br from-emerald-500 to-teal-600 text-[11px] font-bold text-white"
      style={{ boxShadow: "0 1px 3px rgba(16,185,129,0.35)" }}
    >
      L
    </span>
  );
}

function ThemeToggle() {
  const t = useTranslations("nav");
  const [dark, setDark] = useState(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR hydration guard: reads DOM class unavailable during server render
    setDark(document.documentElement.classList.contains("dark"));
  }, []);

  function toggle() {
    const next = !dark;
    setDark(next);
    if (next) {
      document.documentElement.classList.add("dark");
      localStorage.setItem("theme", "dark");
    } else {
      document.documentElement.classList.remove("dark");
      localStorage.setItem("theme", "light");
    }
  }

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={t("theme.toggle")}
      aria-pressed={dark}
      className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-content-muted hover:bg-interactive-secondary hover:text-content-primary transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-interactive-primary"
    >
      {dark ? (
        // Sun icon
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
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
      ) : (
        // Moon icon
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
    </button>
  );
}

function RailToggle({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const t = useTranslations("nav");
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={collapsed ? t("rail.expand") : t("rail.collapse")}
      aria-pressed={collapsed}
      className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-content-muted transition-colors hover:bg-interactive-secondary hover:text-content-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-interactive-primary"
    >
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={["transition-transform duration-150", collapsed ? "rotate-180" : ""].join(" ")}
      >
        <polyline points="11 17 6 12 11 7" />
        <polyline points="18 17 13 12 18 7" />
      </svg>
    </button>
  );
}

export default function Shell({ children }: ShellProps) {
  const t = useTranslations("nav");
  const pathname = useLocation().pathname ?? "/";
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reads localStorage, unavailable during server render
    setCollapsed(readStoredRailCollapsed());
  }, []);

  function toggleCollapsed() {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(RAIL_COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        // localStorage unavailable (private browsing) — collapse state just won't persist
      }
      return next;
    });
  }

  return (
    <div className="flex min-h-screen bg-surface-base text-content-primary">
      {/* Desktop: persistent left rail */}
      <aside
        aria-label={t("primary.ariaLabel")}
        className={[
          "sticky top-0 hidden h-screen shrink-0 flex-col border-r border-edge bg-surface-nav transition-[width] duration-150 md:flex",
          collapsed ? "w-14" : "w-56",
        ].join(" ")}
      >
        <Link
          to="/"
          title={t("dashboard.title")}
          className="flex h-11 shrink-0 items-center gap-2 border-b border-edge px-3"
        >
          <BrandMark />
          {!collapsed && (
            <span className="truncate text-[13px] font-semibold tracking-tight text-content-primary">
              {t("brand.name")}
            </span>
          )}
        </Link>
        <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2 py-2">
          {NAV_GROUPS.map((group) => (
            <NavGroup key={group.label} group={group} pathname={pathname} collapsed={collapsed} />
          ))}
        </nav>
        <div className="flex shrink-0 items-center justify-end border-t border-edge px-2 py-1.5">
          <RailToggle collapsed={collapsed} onToggle={toggleCollapsed} />
        </div>
      </aside>

      {/* Mobile: off-canvas drawer + backdrop */}
      {mobileOpen && (
        <>
          <div
            aria-hidden="true"
            onClick={() => setMobileOpen(false)}
            className="fixed inset-0 z-40 bg-black/40 md:hidden"
          />
          <aside
            aria-label={t("primary.ariaLabel")}
            className="fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-edge bg-surface-nav shadow-card md:hidden"
          >
            <div className="flex h-11 shrink-0 items-center gap-2 border-b border-edge px-3">
              <BrandMark />
              <span className="truncate text-[13px] font-semibold tracking-tight text-content-primary">
                {t("brand.name")}
              </span>
              <button
                type="button"
                aria-label={t("mobile.close")}
                onClick={() => setMobileOpen(false)}
                className="ml-auto flex h-7 w-7 items-center justify-center rounded text-content-muted hover:bg-interactive-secondary hover:text-content-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-interactive-primary"
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
            <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2 py-2">
              {NAV_GROUPS.map((group) => (
                <NavGroup
                  key={group.label}
                  group={group}
                  pathname={pathname}
                  collapsed={false}
                  onNavigate={() => setMobileOpen(false)}
                />
              ))}
            </nav>
          </aside>
        </>
      )}

      {/* Main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header
          className="sticky top-0 z-30 flex h-11 shrink-0 items-center gap-3 border-b border-edge bg-surface-nav px-4"
          style={{ boxShadow: "var(--shadow-header)" }}
        >
          <button
            type="button"
            aria-label={t("mobile.open")}
            aria-expanded={mobileOpen}
            onClick={() => setMobileOpen(true)}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded text-content-muted transition-colors hover:bg-interactive-secondary hover:text-content-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-interactive-primary md:hidden"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <line x1="3" y1="6" x2="21" y2="6" />
              <line x1="3" y1="12" x2="21" y2="12" />
              <line x1="3" y1="18" x2="21" y2="18" />
            </svg>
          </button>

          {/* Mobile-only brand mark — the desktop rail already shows one */}
          <Link to="/" title={t("dashboard.title")} className="flex items-center md:hidden">
            <BrandMark />
          </Link>

          <div className="flex-1" />

          <div className="flex items-center gap-1.5">
            <Suspense fallback={null}>
              <ProjectChip />
            </Suspense>
            <ThemeToggle />
          </div>
        </header>

        <Breadcrumb />

        <div id="main-content" tabIndex={-1} className="w-full">
          {children}
        </div>
      </div>
    </div>
  );
}
