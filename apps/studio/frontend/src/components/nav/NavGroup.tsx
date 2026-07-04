import { Link } from "@tanstack/react-router";
import { useId, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import { GROUP_I18N_KEY, ITEM_I18N_KEY, type NavGroupDef, isRouteActive } from "./types";

interface NavGroupProps {
  group: NavGroupDef;
  pathname: string;
  /** Icon-only rail: hover/click reveals a flyout instead of an inline accordion. */
  collapsed?: boolean;
  onNavigate?: () => void;
}

export default function NavGroup({
  group,
  pathname,
  collapsed = false,
  onNavigate,
}: NavGroupProps) {
  const t = useTranslations("nav");
  const groupActive = group.items.some((item) => isRouteActive(item, pathname));
  const [open, setOpen] = useState(groupActive);
  // Auto-expand the accordion when navigation lands inside this group — this
  // compares against the previous render rather than an effect (React's
  // "adjusting state" pattern) so it never force-collapses on navigating
  // away: an operator who opened a group stays in control of what else is
  // open.
  const [prevGroupActive, setPrevGroupActive] = useState(groupActive);
  if (groupActive !== prevGroupActive) {
    setPrevGroupActive(groupActive);
    if (groupActive) setOpen(true);
  }

  const tGroup = (label: string) => {
    const key = GROUP_I18N_KEY[label];
    return key ? t(key as Parameters<typeof t>[0]) : label;
  };

  const tItem = (label: string) => {
    const key = ITEM_I18N_KEY[label];
    return key ? t(key as Parameters<typeof t>[0]) : label;
  };

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // closeTimerRef must be declared unconditionally at the top (React
  // rules-of-hooks) even though only the collapsed-rail flyout branch uses it.
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuId = useId();

  // Single-item group (Home) — a direct link row, no expand affordance.
  if (group.items.length === 1 && group.items[0].href === "/") {
    const item = group.items[0];
    const active = isRouteActive(item, pathname);
    return (
      <Link
        to={item.href}
        onClick={onNavigate}
        title={collapsed ? tGroup(group.label) : undefined}
        className={[
          "flex h-9 items-center gap-2.5 rounded px-2.5 text-label transition-colors duration-150 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-interactive-primary",
          active
            ? "bg-status-selected-bg font-semibold text-content-primary"
            : "text-content-secondary hover:bg-surface-overlay hover:text-content-primary",
        ].join(" ")}
      >
        <span aria-hidden="true" className="w-4 shrink-0 text-center">
          {group.icon}
        </span>
        {!collapsed && <span className="truncate">{tGroup(group.label)}</span>}
      </Link>
    );
  }

  // Collapsed rail: icon-only trigger, flyout to the right (hover-open with a
  // short delay, close-delay forgiveness for the traversal to the menu).
  if (collapsed) {
    function handleMouseEnter() {
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
      <div className="relative" onMouseEnter={handleMouseEnter} onMouseLeave={handleMouseLeave}>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          onKeyDown={handleTriggerKeyDown}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls={menuId}
          title={tGroup(group.label)}
          className={[
            "flex h-9 w-full items-center justify-center rounded transition-colors duration-150 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-interactive-primary",
            groupActive || open
              ? "bg-status-selected-bg text-content-primary"
              : "text-content-muted hover:bg-surface-overlay hover:text-content-secondary",
          ].join(" ")}
        >
          <span aria-hidden="true">{group.icon}</span>
        </button>

        {open && (
          // eslint-disable-next-line jsx-a11y/interactive-supports-focus -- TODO(#1020 follow-up): menu container; focus goes to menuitem children per ARIA spec
          <div
            ref={menuRef}
            id={menuId}
            role="menu"
            aria-label={group.label}
            onKeyDown={handleMenuKeyDown}
            className="absolute left-full top-0 z-50 ml-1 min-w-44 rounded border border-edge bg-surface-nav py-1 shadow-card"
          >
            <div className="px-3 py-1 text-meta font-medium text-content-muted">
              {tGroup(group.label)}
            </div>
            {group.items.map((item) => {
              const active = isRouteActive(item, pathname);
              return (
                <Link
                  key={item.href}
                  to={item.href}
                  role="menuitem"
                  title={item.tooltip}
                  onClick={() => setOpen(false)}
                  className={[
                    "block whitespace-nowrap px-3 py-2 text-body transition-colors duration-150 hover:bg-surface-overlay focus:bg-surface-overlay focus:outline-none",
                    active ? "font-semibold text-content-primary" : "text-content-secondary",
                  ].join(" ")}
                >
                  {tItem(item.label)}
                </Link>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  // Expanded rail (desktop persistent + mobile drawer): vertical accordion.
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex h-9 w-full items-center gap-2.5 rounded px-2.5 text-label transition-colors duration-150 hover:bg-surface-overlay focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-interactive-primary"
      >
        <span aria-hidden="true" className="w-4 shrink-0 text-center">
          {group.icon}
        </span>
        <span
          className={[
            "flex-1 truncate text-left",
            groupActive ? "font-semibold text-content-primary" : "text-content-secondary",
          ].join(" ")}
        >
          {tGroup(group.label)}
        </span>
        <span
          aria-hidden="true"
          className={["shrink-0 transition-transform duration-150", open ? "rotate-180" : ""].join(
            " ",
          )}
        >
          ▾
        </span>
      </button>
      {open && (
        <div className="flex flex-col gap-0.5 py-0.5 pl-[26px]">
          {group.items.map((item) => {
            const active = isRouteActive(item, pathname);
            return (
              <Link
                key={item.href}
                to={item.href}
                title={item.tooltip}
                onClick={onNavigate}
                className={[
                  "rounded px-2.5 py-1.5 text-body transition-colors duration-150 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-interactive-primary",
                  active
                    ? "bg-status-selected-bg font-semibold text-content-primary"
                    : "text-content-secondary hover:bg-surface-overlay hover:text-content-primary",
                ].join(" ")}
              >
                {tItem(item.label)}
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
