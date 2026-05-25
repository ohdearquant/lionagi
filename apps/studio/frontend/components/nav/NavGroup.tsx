"use client";

import Link from "next/link";
import { useId, useRef, useState } from "react";
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
  // closeTimerRef must be declared unconditionally at the top (React
  // rules-of-hooks) even though only the desktop dropdown branch uses it.
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuId = useId();
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

  // Desktop dropdown: 200ms hover-open delay, 150ms close-delay forgiveness
  // window. The dropdown sits flush below the trigger (top: 100%), so
  // mouseleave on the parent fires as soon as the cursor leaves the
  // trigger row even when it's still heading toward a menu item. The
  // close-delay makes that traversal forgiving.
  function handleMouseEnter() {
    // Cancel any in-flight close so re-entering keeps the menu open.
    if (closeTimerRef.current) {
      clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    timerRef.current = setTimeout(() => setOpen(true), 200);
  }

  function handleMouseLeave() {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    closeTimerRef.current = setTimeout(() => setOpen(false), 150);
  }

  function focusItem(index: number) {
    const items = menuRef.current?.querySelectorAll<HTMLAnchorElement>("[role='menuitem']");
    if (!items || items.length === 0) return;
    const i = (index + items.length) % items.length;
    items[i]?.focus();
  }

  function handleTriggerKeyDown(e: React.KeyboardEvent<HTMLButtonElement>) {
    if (e.key === "Escape") {
      setOpen(false);
      return;
    }
    if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setOpen(true);
      // Wait a tick so the menu is mounted before we focus into it.
      setTimeout(() => focusItem(0), 0);
    }
  }

  function handleMenuKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    const items = menuRef.current?.querySelectorAll<HTMLAnchorElement>("[role='menuitem']");
    if (!items || items.length === 0) return;
    const active = document.activeElement as HTMLElement | null;
    const currentIndex = Array.from(items).findIndex((el) => el === active);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      focusItem(currentIndex + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      focusItem(currentIndex - 1);
    } else if (e.key === "Home") {
      e.preventDefault();
      focusItem(0);
    } else if (e.key === "End") {
      e.preventDefault();
      focusItem(items.length - 1);
    } else if (e.key === "Escape" || e.key === "Tab") {
      setOpen(false);
    }
  }

  return (
    <div
      className="relative flex items-stretch"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        onKeyDown={handleTriggerKeyDown}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
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
        // eslint-disable-next-line jsx-a11y/interactive-supports-focus -- TODO(#1020 follow-up): menu container; focus goes to menuitem children per ARIA spec
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={group.label}
          onKeyDown={handleMenuKeyDown}
          className="absolute left-0 top-full z-50 min-w-44 rounded border border-edge bg-surface-nav py-1 shadow-card"
        >
          {group.items.map((item) => {
            const active = isRouteActive(item, pathname);
            return (
              <Link
                key={item.href}
                href={item.href}
                role="menuitem"
                onClick={() => setOpen(false)}
                className={[
                  "block whitespace-nowrap px-3 py-2 text-body transition-colors duration-150 hover:bg-surface-overlay focus:bg-surface-overlay focus:outline-none",
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
