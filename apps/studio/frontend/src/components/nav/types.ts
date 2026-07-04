import type { LinkProps } from "@tanstack/react-router";

export interface NavItem {
  label: string;
  href: LinkProps["to"];
  /** Single glyph shown in the rail. */
  icon: string;
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
    icon: "▶",
    i18nKey: "surfaces.operations",
    match: (pathname) => pathname === "/",
  },
  {
    label: "Library",
    href: "/library",
    icon: "▤",
    i18nKey: "surfaces.library",
  },
  {
    label: "System",
    href: "/system",
    icon: "⚙",
    i18nKey: "surfaces.system",
  },
];
