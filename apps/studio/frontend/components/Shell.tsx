"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useEffect, useState, type ReactNode } from "react";
import Breadcrumb from "@/components/nav/Breadcrumb";
import NavGroup from "@/components/nav/NavGroup";
import ProjectChip from "@/components/nav/ProjectChip";
import { NAV_GROUPS } from "@/components/nav/types";

export interface ShellProps {
  children: ReactNode;
}

function ThemeToggle() {
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
      aria-label="Toggle theme"
      aria-pressed={dark}
      className="ml-1 flex h-6 w-6 shrink-0 items-center justify-center rounded text-content-muted hover:bg-interactive-secondary hover:text-content-primary transition-colors"
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

export default function Shell({ children }: ShellProps) {
  const pathname = usePathname() ?? "/";
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <div className="min-h-screen bg-surface-base text-content-primary">
      {/* Skip-to-main: sr-only by default; visible on keyboard focus */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:left-2 focus:top-2 focus:z-[9999] focus:rounded focus:bg-surface-raised focus:px-3 focus:py-1.5 focus:text-body focus:text-content-primary focus:shadow-card-hover focus:outline-none focus:ring-2 focus:ring-interactive-primary"
      >
        Skip to main content
      </a>
      <header
        className="sticky top-0 z-40 border-b border-edge bg-surface-nav"
        style={{ boxShadow: "var(--shadow-header)" }}
      >
        <div className="flex h-11 w-full items-stretch gap-5 px-4">
          {/* Brand: monogram + wordmark */}
          <Link
            href="/"
            title="Dashboard"
            className="group flex shrink-0 items-center gap-2 self-center"
          >
            <span
              aria-hidden="true"
              className="flex h-6 w-6 items-center justify-center rounded bg-gradient-to-br from-emerald-500 to-teal-600 text-[11px] font-bold text-white transition-transform duration-150 group-hover:scale-105"
              style={{ boxShadow: "0 1px 3px rgba(16,185,129,0.35)" }}
            >
              L
            </span>
            <span className="flex flex-col leading-tight">
              <span className="text-[13px] font-semibold tracking-tight text-content-primary">
                Lion Studio
              </span>
              <span className="hidden text-[9px] font-medium uppercase tracking-[0.12em] text-content-muted sm:inline">
                Orchestration
              </span>
            </span>
          </Link>

          {/* Desktop: 4-group primary nav */}
          <nav aria-label="Primary" className="hidden items-stretch gap-0.5 md:flex">
            {NAV_GROUPS.map((group) => (
              <NavGroup key={group.label} group={group} pathname={pathname} />
            ))}
          </nav>

          {/* Spacer */}
          <div className="flex-1" />

          {/* Right cluster */}
          <div className="flex items-center gap-1.5 self-center">
            <Suspense fallback={null}>
              <ProjectChip />
            </Suspense>
            <ThemeToggle />
            {/* Hamburger: visible below 768px */}
            <button
              type="button"
              aria-label="Open navigation menu"
              aria-expanded={mobileOpen}
              onClick={() => setMobileOpen((v) => !v)}
              className="flex h-8 w-8 items-center justify-center rounded text-content-muted transition-colors hover:bg-interactive-secondary hover:text-content-primary md:hidden"
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
                {mobileOpen ? (
                  <>
                    <line x1="18" y1="6" x2="6" y2="18" />
                    <line x1="6" y1="6" x2="18" y2="18" />
                  </>
                ) : (
                  <>
                    <line x1="3" y1="6" x2="21" y2="6" />
                    <line x1="3" y1="12" x2="21" y2="12" />
                    <line x1="3" y1="18" x2="21" y2="18" />
                  </>
                )}
              </svg>
            </button>
          </div>
        </div>

        {/* Mobile drawer: vertical accordion of 4 groups */}
        {mobileOpen && (
          <div className="border-t border-edge bg-surface-nav shadow-card md:hidden">
            {NAV_GROUPS.map((group) => (
              <NavGroup
                key={group.label}
                group={group}
                pathname={pathname}
                mobile
                onNavigate={() => setMobileOpen(false)}
              />
            ))}
          </div>
        )}
      </header>

      <Breadcrumb />

      <div id="main-content" tabIndex={-1} className="w-full">
        {children}
      </div>
    </div>
  );
}
