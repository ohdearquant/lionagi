/**
 * Attention digest — compact section of Mission Control.
 *
 * Actionable items (gated, stuck) get individual rows with one-click open.
 * Informational items (failed, stale) collapse into one digest row per
 * reason — count + latest + link into History — never a wall of red.
 */

import type { CSSProperties, ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import SectionLabel from "@/components/ui/SectionLabel";
import Chip from "@/components/ui/Chip";
import Skeleton from "@/components/ui/Skeleton";
import type { AttentionItem, AttentionReason } from "./boardReducer";
import { runDeepLink, invocationDeepLink, scheduleDeepLink } from "@/lib/runDeepLink";

/** Placeholder row count while the first fetch is in flight. */
const SKELETON_ROWS = 3;

interface Props {
  items: AttentionItem[];
  nowSec: number;
  dataState: "loading" | "live" | "stale" | "error";
}

/** Individual rows are reserved for actionable items; overflow lives in History. */
const MAX_ACTIONABLE_ROWS = 6;

/** Shimmering row placeholders, sized to match a real AttentionRow. */
export function AttentionQueueSkeleton() {
  return (
    <div aria-hidden="true">
      <div className="mb-2 flex items-center justify-between">
        <Skeleton className="h-4 w-28" />
        <Skeleton className="h-3 w-16" />
      </div>
      <div className="overflow-hidden rounded border border-edge">
        {Array.from({ length: SKELETON_ROWS }, (_, i) => (
          <div
            key={i}
            className="flex items-center gap-3 bg-surface-raised px-3 py-2"
            style={{ borderTop: i === 0 ? undefined : "1px solid var(--edge-hairline)" }}
          >
            <Skeleton className="h-3 w-16 shrink-0" />
            <Skeleton className="h-3 flex-1" />
            <Skeleton className="h-5 w-12 shrink-0 rounded" />
            <Skeleton className="h-3 w-8 shrink-0" />
          </div>
        ))}
      </div>
    </div>
  );
}

const ACTIONABLE_REASONS: ReadonlySet<AttentionReason> = new Set(["streak", "gated", "stuck"]);

const REASON_COLOR: Record<AttentionReason, string> = {
  streak: "var(--status-failure)",
  failed: "var(--status-failure)",
  stale: "var(--status-pending)",
  stuck: "var(--status-pending)",
  gated: "var(--accent)",
};

function elapsedLabel(startedAt: number | null, nowSec: number): string {
  if (startedAt == null) return "—";
  // Timestamps are float epochs — floor so sub-minute ages never render
  // fractional seconds.
  const s = Math.max(0, Math.floor(nowSec - startedAt));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m - h * 60;
  return mm > 0 ? `${h}h ${mm}m` : `${h}h`;
}

export default function AttentionQueue({ items, nowSec }: Props) {
  const t = useTranslations("mission");

  const actionable = items.filter((i) => ACTIONABLE_REASONS.has(i.reason));
  const digests: { reason: AttentionReason; group: AttentionItem[] }[] = (
    ["failed", "stale"] as const
  )
    .map((reason) => ({ reason, group: items.filter((i) => i.reason === reason) }))
    .filter((d) => d.group.length > 0);

  return (
    <section aria-labelledby="attention-heading">
      <div className="mb-2 flex items-center justify-between">
        <SectionLabel
          trailing={
            <span
              className="rounded px-1.5 py-0.5 font-data text-[length:var(--t-xs)] font-semibold tabular-nums"
              style={{
                background: "color-mix(in srgb, var(--accent) 15%, transparent)",
                color: "var(--accent)",
              }}
            >
              {items.length}
            </span>
          }
        >
          <span id="attention-heading">{t("attention.title")}</span>
        </SectionLabel>
        <Link
          to="/history"
          className="font-data text-[length:var(--t-xs)] text-content-muted transition-colors duration-100"
        >
          {t("attention.viewAll")}
        </Link>
      </div>

      <div className="overflow-hidden rounded border border-edge">
        {actionable.slice(0, MAX_ACTIONABLE_ROWS).map((item, idx) => (
          <AttentionRow key={item.id} item={item} nowSec={nowSec} first={idx === 0} />
        ))}
        {actionable.length > MAX_ACTIONABLE_ROWS && (
          <Link
            to="/history"
            className="flex items-center justify-center bg-surface-raised px-3 py-2 font-data text-[length:var(--t-xs)] text-content-muted transition-colors duration-100"
            style={{ borderTop: "1px solid var(--edge-hairline)" }}
          >
            {t("attention.more", { count: actionable.length - MAX_ACTIONABLE_ROWS })}
          </Link>
        )}
        {digests.map(({ reason, group }, idx) => (
          <div key={reason}>
            <DigestRow
              reason={reason}
              group={group}
              nowSec={nowSec}
              first={idx === 0 && actionable.length === 0}
            />
            {/* The digest stays one line, but the freshest failures are
                directly openable — a count alone isn't actionable. */}
            {reason === "failed" &&
              group
                .slice(0, 3)
                .map((item) => (
                  <AttentionRow key={item.id} item={item} nowSec={nowSec} first={false} />
                ))}
          </div>
        ))}
      </div>
    </section>
  );
}

/** One line per reason: count + most recent item + age, linking into History. */
function DigestRow({
  reason,
  group,
  nowSec,
  first,
}: {
  reason: AttentionReason;
  group: AttentionItem[];
  nowSec: number;
  first: boolean;
}) {
  const t = useTranslations("mission");
  const latest = group[0];
  return (
    <Link
      to="/history"
      className="flex items-center gap-3 bg-surface-raised px-3 py-2 transition-colors duration-100 hover:bg-surface-overlay"
      style={{ borderTop: first ? undefined : "1px solid var(--edge-hairline)" }}
    >
      <span
        className="shrink-0 font-data text-[length:var(--t-xs)] font-semibold uppercase tracking-wider"
        style={{ color: REASON_COLOR[reason], minWidth: 90 }}
      >
        {t(`attention.reason.${reason}` as Parameters<typeof t>[0])}
      </span>
      <span className="shrink-0 font-data tabular-nums text-[length:var(--t-xs)] font-semibold text-content-secondary">
        {group.length}
      </span>
      <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-xs)] text-content-muted">
        {t("attention.digestLatest", {
          name: latest.name,
          age: elapsedLabel(latest.startedAt, nowSec),
        })}
      </span>
      <span className="shrink-0 text-[length:var(--t-xs)] text-content-muted">
        {t("attention.viewAll")}
      </span>
    </Link>
  );
}

function ItemLink({
  item,
  className,
  style,
  children,
}: {
  item: AttentionItem;
  className?: string;
  style?: CSSProperties;
  children: ReactNode;
}) {
  const id = item.id.slice(item.id.indexOf(":") + 1);
  if (item.kind === "run") {
    return (
      <Link {...runDeepLink(id)} className={className} style={style}>
        {children}
      </Link>
    );
  }
  if (item.kind === "schedule") {
    return (
      <Link {...scheduleDeepLink(id)} className={className} style={style}>
        {children}
      </Link>
    );
  }
  return (
    <Link {...invocationDeepLink()} className={className} style={style}>
      {children}
    </Link>
  );
}

function AttentionRow({
  item,
  nowSec,
  first,
}: {
  item: AttentionItem;
  nowSec: number;
  first: boolean;
}) {
  const t = useTranslations("mission");
  const color = REASON_COLOR[item.reason] ?? "var(--accent)";
  return (
    <div
      className="flex items-center gap-3 bg-surface-raised px-3 py-2 transition-colors duration-100"
      style={{ borderTop: first ? undefined : "1px solid var(--edge-hairline)" }}
    >
      {/* Reason indicator — color is data-driven from REASON_COLOR map */}
      <span
        className="shrink-0 font-data text-[length:var(--t-xs)] font-semibold uppercase tracking-wider"
        style={{ color, minWidth: 90 }}
      >
        {t(`attention.reason.${item.reason}` as Parameters<typeof t>[0])}
      </span>

      {/* Name + optional one-line failure reason */}
      <div className="flex min-w-0 flex-1 items-baseline gap-2">
        <ItemLink
          item={item}
          className="min-w-0 max-w-full shrink truncate font-data text-[length:var(--t-sm)] text-content-primary transition-opacity duration-100 hover:opacity-70"
        >
          {item.name}
        </ItemLink>
        {item.reasonSummary && (
          <span
            className="min-w-0 flex-1 truncate font-data text-[length:var(--t-xs)] text-content-muted"
            title={item.reasonSummary}
          >
            {item.reasonSummary}
          </span>
        )}
      </div>

      {/* Consecutive-failure count on streak rows */}
      {item.streakCount != null && (
        <span
          className="shrink-0 font-data tabular-nums text-[length:var(--t-xs)] font-semibold"
          style={{ color: "var(--status-failure)" }}
        >
          {t("attention.streakCount", { count: item.streakCount })}
        </span>
      )}

      {/* Kind badge */}
      <Chip mono className="shrink-0">
        {item.kind}
      </Chip>

      {/* Age (ticking) */}
      <span className="min-w-[40px] shrink-0 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
        {elapsedLabel(item.startedAt, nowSec)}
      </span>

      {/* Action — color-mix tint stays inline per app-wide pattern */}
      <ItemLink
        item={item}
        className="shrink-0 rounded px-2 py-1 font-data text-[length:var(--t-xs)] font-semibold transition-colors duration-100"
        style={{
          background: "color-mix(in srgb, var(--accent) 12%, transparent)",
          color: "var(--accent)",
          border: "1px solid color-mix(in srgb, var(--accent) 25%, transparent)",
        }}
      >
        {t("attention.open")}
      </ItemLink>
    </div>
  );
}
