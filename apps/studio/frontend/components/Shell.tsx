"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

type NavItem = {
  label: string;
  href: string;
  match?: (pathname: string) => boolean;
};

const navItems: NavItem[] = [
  { label: "Playbooks", href: "/playbooks" },
  { label: "Agents", href: "/agents" },
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

export default function Shell({ children }: ShellProps) {
  const pathname = usePathname() ?? "/";

  return (
    <div className="min-h-screen bg-surface-base text-content-primary">
      <header className="sticky top-0 z-40 border-b border-edge bg-surface-nav/95 backdrop-blur">
        <div className="mx-auto flex w-full max-w-7xl flex-col gap-3 px-4 py-2.5 lg:flex-row lg:items-center lg:justify-between">
          <Link
            href="/"
            className="flex min-w-0 flex-col text-content-primary hover:text-content-inverse"
          >
            <span className="text-sm font-semibold uppercase tracking-wider">Lion Studio</span>
            <span className="truncate text-[11px] text-content-muted">playbook orchestration</span>
          </Link>

          <nav aria-label="Primary" className="flex gap-1 overflow-x-auto pb-1 text-sm lg:pb-0">
            {navItems.map((item) => {
              const active = isActive(item, pathname);

              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={[
                    "whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                    active
                      ? "bg-interactive-secondary text-content-inverse"
                      : "text-content-secondary hover:bg-interactive-secondary/50 hover:text-content-primary",
                  ].join(" ")}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </div>
      </header>

      <div className="mx-auto w-full max-w-7xl px-4 py-6">{children}</div>
    </div>
  );
}
