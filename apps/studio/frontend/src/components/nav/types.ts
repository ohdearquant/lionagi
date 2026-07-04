import type { LinkProps } from "@tanstack/react-router";
import { IconTarget, IconGrid, IconGear } from "@/components/icons";

export interface NavItem {
  label: string;
  href: LinkProps["to"];
  /** One-SVG-contract icon shown in the rail (DESIGN-SYSTEM.md §6). */
  Icon: typeof IconTarget;
  /** i18n key under the `nav.surfaces` namespace. */
  i18nKey: string;
  match?: (pathname: string) => boolean;
}

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

// ADR-0093: the rail is exactly three flat surfaces — no groups, no
// children, no per-entity nav items. Every former page is now a
// URL-addressable state (params, filters, slide-overs) of one of these.
export const NAV_ITEMS: NavItem[] = [
  {
    label: "Operations",
    href: "/",
    Icon: IconTarget,
    i18nKey: "surfaces.operations",
    match: (pathname) => pathname === "/",
  },
  {
    label: "Library",
    href: "/library",
    Icon: IconGrid,
    i18nKey: "surfaces.library",
  },
  {
    label: "System",
    href: "/system",
    Icon: IconGear,
    i18nKey: "surfaces.system",
  },
];
