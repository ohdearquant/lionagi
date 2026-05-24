"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { type NavGroupDef, isRouteActive } from "./types";

interface NavGroupProps {
  group: NavGroupDef;
  pathname: string;
  mobile?: boolean;
  onNavigate?: () => void;
}

export default function NavGroup({ group, pathname, mobile = false, onNavigate }: NavGroupProps) {
  const [open, setOpen] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const groupActive = group.items.some((item) => isRouteActive(item, pathname));

  // Dashboard single direct link — no submenu
  if (group.items.length === 1 && group.items[0].href === "/") {
    const item = group.items[0];
    const active = isRouteActive(item, pathname);
    if (mobile) {
      return (
        <div className="border-b border-edge">
          <Link
            href={item.href}
            onClick={onNavigate}
            className={[
              "flex h-10 w-full items-center px-4 text-label transition-colors duration-150",
              active ? "font-semibold text-content-primary" : "text-content-secondary",
            ].join(" ")}
          >
            {group.label}
          </Link>
        </div>
      );
    }
    return (
      <div className="relative flex items-stretch">
        <Link
          href={item.href}
          aria-current={active ? "page" : undefined}
          className={[
            "relative flex items-center whitespace-nowrap px-2.5 text-[12px] font-medium transition-colors duration-150",
            active ? "text-content-primary" : "text-content-muted hover:text-content-secondary",
          ].join(" ")}
        >
          {group.label}
          {active && (
            <span className="absolute inset-x-2.5 bottom-0 h-[2px] rounded-t bg-interactive-primary" />
          )}
        </Link>
      </div>
    );
  }

  // Mobile accordion
  if (mobile) {
    return (
      <div className="border-b border-edge">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex h-10 w-full items-center justify-between px-4 text-label transition-colors duration-150"
        >
          <span
            className={
              groupActive ? "font-semibold text-content-primary" : "text-content-secondary"
            }
          >
            {group.label}
          </span>
          <span
            className={["transition-transform duration-150", open ? "rotate-180" : ""].join(" ")}
          >
            ▾
          </span>
        </button>
        {open && (
          <div className="flex flex-col bg-surface-overlay/50">
            {group.items.map((item) => {
              const active = isRouteActive(item, pathname);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  onClick={onNavigate}
                  className={[
                    "px-6 py-2 text-body transition-colors duration-150",
                    active
                      ? "font-semibold text-content-primary"
                      : "text-content-secondary hover:text-content-primary",
                  ].join(" ")}
                >
                  {item.label}
                </Link>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  // Desktop dropdown with 200ms hover-open delay
  function handleMouseEnter() {
    timerRef.current = setTimeout(() => setOpen(true), 200);
  }

  function handleMouseLeave() {
    if (timerRef.current) clearTimeout(timerRef.current);
    setOpen(false);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") setOpen(false);
  }

  return (
    <div
      className="relative flex items-stretch"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
      onKeyDown={handleKeyDown}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={[
          "relative flex items-center whitespace-nowrap px-2.5 text-[12px] font-medium transition-colors duration-150",
          groupActive || open
            ? "text-content-primary"
            : "text-content-muted hover:text-content-secondary",
        ].join(" ")}
      >
        {group.label} ▾
        {(groupActive || open) && (
          <span className="absolute inset-x-2.5 bottom-0 h-[2px] rounded-t bg-interactive-primary" />
        )}
      </button>

      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 min-w-44 rounded border border-edge bg-surface-nav py-1 shadow-card">
          {group.items.map((item) => {
            const active = isRouteActive(item, pathname);
            return (
              <Link
                key={item.href}
                href={item.href}
                onClick={() => setOpen(false)}
                className={[
                  "block whitespace-nowrap px-3 py-2 text-body transition-colors duration-150 hover:bg-surface-overlay",
                  active ? "font-semibold text-content-primary" : "text-content-secondary",
                ].join(" ")}
              >
                {item.label}
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
