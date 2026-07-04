import { Link, useLocation, type LinkProps } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import { Suspense, useEffect, useState, type ReactNode } from "react";
import Breadcrumb from "@/components/nav/Breadcrumb";
import ProjectChip from "@/components/nav/ProjectChip";
import { NAV_ITEMS, isRouteActive } from "@/components/nav/types";
import {
  IconChevrons,
  IconClose,
  IconMenu,
  IconMoon,
  IconSparkle,
  IconSun,
} from "@/components/icons";

export interface ShellProps {
  children: ReactNode;
}

const RAIL_COLLAPSED_KEY = "studio.railCollapsed";
const DOCK_EXPANDED_KEY = "studio.dockExpanded";
const THEME_KEY = "theme";

function readStoredRailCollapsed(): boolean {
  try {
    return window.localStorage.getItem(RAIL_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function readStoredDockExpanded(): boolean {
  try {
    return window.localStorage.getItem(DOCK_EXPANDED_KEY) === "1";
  } catch {
    return false;
  }
}

function BrandMark() {
  return (
    <span
      aria-hidden="true"
      className="flex h-6 w-6 shrink-0 items-center justify-center rounded bg-accent text-[11px] font-bold text-accent-contrast"
    >
      L
    </span>
  );
}

function ThemeToggle() {
  const t = useTranslations("nav");
  // Dark is the default; light is the attribute override (DESIGN-SYSTEM.md §1.2).
  const [dark, setDark] = useState(true);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR hydration guard: reads DOM attribute unavailable during server render
    setDark(document.documentElement.getAttribute("data-theme") !== "light");
  }, []);

  function toggle() {
    const next = !dark;
    setDark(next);
    document.documentElement.setAttribute("data-theme", next ? "dark" : "light");
    localStorage.setItem(THEME_KEY, next ? "dark" : "light");
  }

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={t("theme.toggle")}
      aria-pressed={dark}
      className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-content-muted transition-colors hover:bg-surface-overlay hover:text-content-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
    >
      {dark ? <IconSun width={14} height={14} /> : <IconMoon width={14} height={14} />}
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
      className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-content-muted transition-colors hover:bg-surface-overlay hover:text-content-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
    >
      <IconChevrons
        width={14}
        height={14}
        className={["transition-transform duration-150", collapsed ? "rotate-180" : ""].join(" ")}
      />
    </button>
  );
}

function RailLink({
  href,
  Icon,
  label,
  active,
  collapsed,
  onNavigate,
}: {
  href: LinkProps["to"];
  Icon: (typeof NAV_ITEMS)[number]["Icon"];
  label: string;
  active: boolean;
  collapsed: boolean;
  onNavigate?: () => void;
}) {
  return (
    <Link
      to={href}
      onClick={onNavigate}
      title={collapsed ? label : undefined}
      className={[
        "relative flex h-9 items-center gap-2.5 rounded px-2.5 text-label transition-colors duration-150 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent",
        active
          ? "bg-surface-overlay font-semibold text-content-primary"
          : "text-content-secondary hover:bg-surface-overlay hover:text-content-primary",
      ].join(" ")}
    >
      {/* Amber left hairline — the active-item indicator (DESIGN-SYSTEM.md §5),
          not a filled/colored icon. */}
      {active && (
        <span
          aria-hidden="true"
          className="absolute inset-y-1.5 left-0 w-0.5 rounded-r bg-accent"
        />
      )}
      <span aria-hidden="true" className="w-4 shrink-0 text-center">
        <Icon />
      </span>
      {!collapsed && <span className="truncate">{label}</span>}
    </Link>
  );
}

const DOCK_COLLAPSED_WIDTH = "w-9";
const DOCK_EXPANDED_WIDTH = "w-72";

/**
 * Reserved right-side dock slot for the Leo operator panel (ADR-0093): an
 * operator chat that also drives the UI, present on every surface. The
 * live panel + its daemon chat/signals wiring land in phase B — phase A
 * only reserves the collapsible slot so the layout doesn't shift later.
 * This is the one legitimate overlay-drawer surface per the cockpit's
 * master-detail rule (transient cross-space chat, not object detail).
 */
function LeoDock({ expanded, onToggle }: { expanded: boolean; onToggle: () => void }) {
  const t = useTranslations("nav");
  return (
    <aside
      aria-label={t("dock.ariaLabel")}
      className={[
        "sticky top-0 hidden h-screen shrink-0 flex-col border-l border-edge bg-surface-raised transition-[width] duration-150 md:flex",
        expanded ? DOCK_EXPANDED_WIDTH : DOCK_COLLAPSED_WIDTH,
      ].join(" ")}
    >
      <div className="flex h-11 shrink-0 items-center justify-center border-b border-edge px-1.5">
        <button
          type="button"
          onClick={onToggle}
          aria-label={expanded ? t("dock.collapse") : t("dock.expand")}
          aria-pressed={expanded}
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-content-muted transition-colors hover:bg-surface-overlay hover:text-content-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
        >
          <IconSparkle width={14} height={14} />
        </button>
      </div>
      {expanded && (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-4 py-6 text-center">
          <IconSparkle width={20} height={20} className="text-content-muted" />
          <p className="text-label font-medium text-content-secondary">{t("dock.title")}</p>
          <p className="text-meta text-content-muted">{t("dock.comingSoon")}</p>
        </div>
      )}
    </aside>
  );
}

export default function Shell({ children }: ShellProps) {
  const t = useTranslations("nav");
  const pathname = useLocation().pathname ?? "/";
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [dockExpanded, setDockExpanded] = useState(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reads localStorage, unavailable during server render
    setCollapsed(readStoredRailCollapsed());

    setDockExpanded(readStoredDockExpanded());
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

  function toggleDock() {
    setDockExpanded((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(DOCK_EXPANDED_KEY, next ? "1" : "0");
      } catch {
        // localStorage unavailable (private browsing) — expand state just won't persist
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
          "sticky top-0 hidden h-screen shrink-0 flex-col border-r border-edge bg-surface-raised transition-[width] duration-150 md:flex",
          collapsed ? "w-14" : "w-56",
        ].join(" ")}
      >
        <Link
          to="/"
          title={t("surfaces.operations")}
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
          {NAV_ITEMS.map((item) => (
            <RailLink
              key={item.href}
              href={item.href}
              Icon={item.Icon}
              label={t(item.i18nKey as Parameters<typeof t>[0])}
              active={isRouteActive(item, pathname)}
              collapsed={collapsed}
            />
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
            className="fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-edge bg-surface-raised md:hidden"
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
                className="ml-auto flex h-7 w-7 items-center justify-center rounded text-content-muted hover:bg-surface-overlay hover:text-content-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
              >
                <IconClose width={16} height={16} />
              </button>
            </div>
            <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2 py-2">
              {NAV_ITEMS.map((item) => (
                <RailLink
                  key={item.href}
                  href={item.href}
                  Icon={item.Icon}
                  label={t(item.i18nKey as Parameters<typeof t>[0])}
                  active={isRouteActive(item, pathname)}
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
        <header className="sticky top-0 z-30 flex h-11 shrink-0 items-center gap-3 border-b border-edge bg-surface-raised px-4 shadow-raised-soft">
          <button
            type="button"
            aria-label={t("mobile.open")}
            aria-expanded={mobileOpen}
            onClick={() => setMobileOpen(true)}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded text-content-muted transition-colors hover:bg-surface-overlay hover:text-content-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent md:hidden"
          >
            <IconMenu width={18} height={18} />
          </button>

          {/* Mobile-only brand mark — the desktop rail already shows one */}
          <Link to="/" title={t("surfaces.operations")} className="flex items-center md:hidden">
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

      <LeoDock expanded={dockExpanded} onToggle={toggleDock} />
    </div>
  );
}
