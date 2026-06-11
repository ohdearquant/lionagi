import type { ReactNode } from "react";
import type { LinkProps } from "@tanstack/react-router";

export interface NavItem {
  label: string;
  href: LinkProps["to"];
  icon?: ReactNode;
  match?: (pathname: string) => boolean;
}

export interface NavGroup {
  label: string;
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

export const NAV_GROUPS: NavGroupDef[] = [
  {
    label: "Dashboard",
    items: [{ label: "Dashboard", href: "/", match: (pathname) => pathname === "/" }],
  },
  {
    label: "Work",
    items: [
      { label: "Shows", href: "/shows" },
      { label: "Playfield", href: "/playfield" },
      { label: "Runs", href: "/runs" },
      { label: "Kanban", href: "/kanban" },
      { label: "Projects", href: "/projects" },
      { label: "Teams", href: "/teams" },
      { label: "Invocations", href: "/invocations" },
      { label: "Schedules", href: "/schedules" },
    ],
  },
  {
    label: "Library",
    items: [
      { label: "Playbooks", href: "/playbooks" },
      { label: "Agents", href: "/agents" },
      { label: "Plugins", href: "/plugins" },
      { label: "Skills", href: "/skills" },
    ],
  },
  {
    label: "Admin",
    items: [
      { label: "Health", href: "/admin/health" },
      { label: "Maintenance", href: "/admin/maintenance" },
    ],
  },
];
