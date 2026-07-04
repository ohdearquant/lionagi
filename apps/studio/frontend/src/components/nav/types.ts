import type { ReactNode } from "react";
import type { LinkProps } from "@tanstack/react-router";

export interface NavItem {
  label: string;
  href: LinkProps["to"];
  icon?: ReactNode;
  /** Native title-attribute tooltip, e.g. clarifying an item's renamed label. */
  tooltip?: string;
  match?: (pathname: string) => boolean;
}

export interface NavGroup {
  label: string;
  /** Single glyph shown in the rail (both expanded and collapsed states). */
  icon: string;
  items: NavItem[];
  isOpen?: boolean;
}

export type NavGroupDef = Omit<NavGroup, "isOpen">;

export function isRouteActive(item: NavItem, pathname: string): boolean {
  if (item.match) {
    return item.match(pathname);
  }
  const href = item.href;
  if (typeof href !== "string") return false;
  if (href === "/") {
    return pathname === "/";
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}

// The 5 top-level rail destinations (design spec §3). Shows and Projects are
// intentionally not linked from any group — Shows' routes stay reachable by
// direct URL, and Projects is replaced by the project switcher in the top bar.
export const NAV_GROUPS: NavGroupDef[] = [
  {
    label: "Home",
    icon: "◉",
    items: [{ label: "Home", href: "/", match: (pathname) => pathname === "/" }],
  },
  {
    label: "Operations",
    icon: "▶",
    items: [
      { label: "Runs", href: "/runs" },
      { label: "Invocations", href: "/invocations" },
      { label: "Board", href: "/kanban" },
      { label: "Playfield", href: "/playfield" },
    ],
  },
  {
    label: "Automations",
    icon: "⏱",
    items: [{ label: "Schedules", href: "/schedules" }],
  },
  {
    label: "Library",
    icon: "▤",
    items: [
      { label: "Agents", href: "/agents" },
      { label: "Scripts", href: "/playbooks", tooltip: "runnable playbooks · li play" },
      { label: "Skills", href: "/skills" },
      { label: "Plugins", href: "/plugins" },
      { label: "Engines", href: "/engines" },
      { label: "Teams", href: "/teams" },
    ],
  },
  {
    label: "Admin",
    icon: "⚙",
    items: [
      { label: "Overview", href: "/admin", match: (pathname) => pathname === "/admin" },
      { label: "Health", href: "/admin/health" },
      { label: "Maintenance", href: "/admin/maintenance" },
    ],
  },
];

// Shared label → i18n-key maps consumed by both Rail (expanded/collapsed) and
// Breadcrumb, kept in one place so a relabel can't drift between the two.
export const GROUP_I18N_KEY: Record<string, string> = {
  Home: "groups.home",
  Operations: "groups.operations",
  Automations: "groups.automations",
  Library: "groups.library",
  Admin: "groups.admin",
};

export const ITEM_I18N_KEY: Record<string, string> = {
  Home: "items.home",
  Runs: "items.runs",
  Invocations: "items.invocations",
  Board: "items.board",
  Playfield: "items.playfield",
  Schedules: "items.schedules",
  Agents: "items.agents",
  Scripts: "items.scripts",
  Skills: "items.skills",
  Plugins: "items.plugins",
  Engines: "items.engines",
  Teams: "items.teams",
  Overview: "items.overview",
  Health: "items.health",
  Maintenance: "items.maintenance",
};
