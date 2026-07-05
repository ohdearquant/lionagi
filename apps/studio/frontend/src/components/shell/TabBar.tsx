/**
 * TabBar — space-level tab navigation (DESIGN-SYSTEM §11: tabs, not pages).
 *
 * Tabs are URLs: each tab is a Link to a route or a search-param variant of
 * the current route, so back/forward and deep links work. Hairline baseline,
 * 2px amber underline on the active tab. No internal state.
 */

import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

export interface TabSpec {
  /** Stable id; also used as the React key. */
  id: string;
  label: ReactNode;
  /** Route path for this tab. */
  to: string;
  /** Optional search params (for ?tab= style tabs within one route). */
  search?: Record<string, string | undefined>;
  active: boolean;
}

export default function TabBar({ tabs, ariaLabel }: { tabs: TabSpec[]; ariaLabel?: string }) {
  return (
    <nav aria-label={ariaLabel} className="flex items-end gap-1 border-b border-edge">
      {tabs.map((tab) => (
        <Link
          key={tab.id}
          to={tab.to}
          search={tab.search}
          aria-current={tab.active ? "page" : undefined}
          className={`relative px-3 pb-2 pt-1 text-[length:var(--t-sm)] transition-colors duration-100 ${
            tab.active ? "font-semibold text-content-primary" : "font-normal text-content-muted"
          }`}
        >
          {tab.label}
          {tab.active && (
            <span
              aria-hidden
              className="absolute inset-x-2 -bottom-px h-[2px] rounded-full bg-accent"
            />
          )}
        </Link>
      ))}
    </nav>
  );
}
