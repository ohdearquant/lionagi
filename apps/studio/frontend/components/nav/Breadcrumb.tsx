"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { NAV_GROUPS, isRouteActive } from "./types";

export default function Breadcrumb() {
  const pathname = usePathname() ?? "/";

  // Dashboard root
  if (pathname === "/") {
    return (
      <nav
        aria-label="Breadcrumb"
        className="flex h-6 items-center gap-1 border-b border-edge bg-surface-base px-4 text-meta text-content-muted"
      >
        <span>Dashboard</span>
      </nav>
    );
  }

  // Find matching group and item
  let groupLabel: string | null = null;
  let itemLabel: string | null = null;
  let itemHref: string | null = null;

  outer: for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (isRouteActive(item, pathname)) {
        groupLabel = group.label;
        itemLabel = item.label;
        itemHref = item.href;
        break outer;
      }
    }
  }

  if (!groupLabel || !itemLabel || !itemHref) {
    // Unknown route: show first decoded segment
    const firstSegment = pathname.split("/").filter(Boolean)[0] ?? "";
    return (
      <nav
        aria-label="Breadcrumb"
        className="flex h-6 items-center gap-1 border-b border-edge bg-surface-base px-4 text-meta text-content-muted"
      >
        {firstSegment && <span>{decodeURIComponent(firstSegment)}</span>}
      </nav>
    );
  }

  // Determine detail segment beyond the section route
  let detailSegment: string | null = null;
  if (pathname !== itemHref && pathname.startsWith(`${itemHref}/`)) {
    const remainder = pathname.slice(itemHref.length + 1);
    const segment = remainder.split("/")[0];
    if (segment) {
      const decoded = decodeURIComponent(segment);
      detailSegment = decoded.length > 12 ? decoded.slice(0, 12) : decoded;
    }
  }

  return (
    <nav
      aria-label="Breadcrumb"
      className="flex h-6 items-center gap-1 border-b border-edge bg-surface-base px-4 text-meta text-content-muted"
    >
      <span>{groupLabel}</span>
      <span aria-hidden="true">›</span>
      <Link href={itemHref} className="transition-colors duration-150 hover:text-content-secondary">
        {itemLabel}
      </Link>
      {detailSegment && (
        <>
          <span aria-hidden="true">›</span>
          <span>{detailSegment}</span>
        </>
      )}
    </nav>
  );
}
