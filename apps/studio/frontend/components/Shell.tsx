"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

type NavItem = {
  label: string;
  href: string;
  match?: (pathname: string) => boolean;
};

const navItems: NavItem[] = [
  { label: "Playbooks", href: "/playbooks" },
  { label: "Agents", href: "/agents" },
  { label: "Skills", href: "/skills" },
  { label: "Runs", href: "/runs" },
  { label: "Shows", href: "/shows" },
];

function isActive(item: NavItem, pathname: string) {
  if (item.match) {
    return item.match(pathname);
  }

  if (item.href === "/") {
    return pathname === "/";
  }

  return pathname === item.href || pathname.startsWith(`${item.href}/`);
}

export interface ShellProps {
  children: ReactNode;
}

function ThemeToggle() {
  const [dark, setDark] = useState(false);

  useEffect(() => {
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
      className="ml-1 flex h-6 w-6 shrink-0 items-center justify-center rounded text-content-muted hover:bg-interactive-secondary hover:text-content-primary transition-colors"
    >
      {dark ? (
        // Sun icon
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="5"/>
          <line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>
          <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
          <line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>
          <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
        </svg>
      ) : (
        // Moon icon
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
        </svg>
      )}
    </button>
  );
}

export default function Shell({ children }: ShellProps) {
  const pathname = usePathname() ?? "/";

  return (
    <div className="min-h-screen bg-surface-base text-content-primary">
      <header className="sticky top-0 z-40 border-b border-edge bg-surface-nav">
        <div className="flex h-11 w-full items-stretch gap-5 px-4">
          {/* Brand: monogram + wordmark */}
          <Link
            href="/"
            className="group flex shrink-0 items-center gap-2.5 self-center"
          >
            <span
              aria-hidden="true"
              className="flex h-6 w-6 items-center justify-center rounded-md bg-gradient-to-br from-emerald-500 to-teal-600 text-[11px] font-bold text-white shadow-sm transition-transform group-hover:scale-105"
            >
              L
            </span>
            <span className="flex flex-col leading-tight">
              <span className="text-[13px] font-semibold tracking-tight text-content-primary">
                Lion Studio
              </span>
              <span className="hidden text-[9px] font-medium uppercase tracking-[0.12em] text-content-muted sm:inline">
                Playbook Orchestration
              </span>
            </span>
          </Link>

          {/* Tab-style primary nav with underline */}
          <nav aria-label="Primary" className="flex items-stretch gap-0.5">
            {navItems.map((item) => {
              const active = isActive(item, pathname);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={[
                    "relative flex items-center whitespace-nowrap px-2.5 text-[12px] font-medium transition-colors",
                    active
                      ? "text-content-primary"
                      : "text-content-muted hover:text-content-secondary",
                  ].join(" ")}
                >
                  {item.label}
                  {active && (
                    <span className="absolute inset-x-2 bottom-0 h-[2px] rounded-t bg-interactive-primary" />
                  )}
                </Link>
              );
            })}
          </nav>

          {/* Spacer */}
          <div className="flex-1" />

          {/* Right cluster */}
          <div className="flex items-center gap-1.5 self-center">
            <ThemeToggle />
          </div>
        </div>
      </header>

      <div className="w-full">{children}</div>
    </div>
  );
}
