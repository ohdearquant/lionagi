/**
 * Attention digest — compact section of Mission Control.
 *
 * Actionable items (gated, stuck) get individual rows with one-click open.
 * Informational items (failed, stale) collapse into one digest row per
 * reason — count + latest + link into the dedicated Attention tab, never a
 * wall of red. Orphaned (daemon-restart housekeeping) runs never reach the
 * attention list at all — they surface only in the Recent history strip as
 * a neutral chip, so nothing here is pure housekeeping noise.
 *
 * Discharge lifecycle: every row also offers Acknowledge/Resolve/Snooze/
 * Expected — except "gated" rows, which offer Acknowledge only. A gate is
 * resolved by the actual approve/reject action on the run, not discharged
 * out of this list; letting it be snoozed/resolved here would hide a
 * pending approval without anyone having acted on it.
 *
 * These persist server-side (see boardReducer's dispositions join) — a
 * discharged (resolved/expected/snoozed) item leaves this default view on
 * the next poll, but stays queryable via "Show discharged" below or the
 * full Attention tab. Acknowledged items stay visible here, only restyled:
 * acknowledging is "seen, not fixed," never a hide.
 */

import { useState, type CSSProperties, type FormEvent, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import SectionLabel from "@/components/ui/SectionLabel";
import Chip from "@/components/ui/Chip";
import Skeleton from "@/components/ui/Skeleton";
import { type AttentionItem, type AttentionReason } from "./boardReducer";
import { runDeepLink, invocationDeepLink, scheduleDeepLink, playDeepLink } from "@/lib/runDeepLink";
import { putAttentionDisposition, deleteAttentionDisposition, ApiError } from "@/lib/api";
import type { AttentionDispositionState } from "@/lib/api";
import { formatElapsed } from "@/lib/elapsed";

/** Placeholder row count while the first fetch is in flight. */
const SKELETON_ROWS = 3;

interface Props {
  items: AttentionItem[];
  /** Discharged (resolved/expected/snoozed) items — hidden by default. */
  dischargedItems: AttentionItem[];
  /** Items still awaiting a disposition. Passed in rather than derived here so
   * that this heading and the page summary above it render the same number
   * from the same computation — they used to disagree, because the heading
   * counted every active item while the summary counted only unanswered ones,
   * and the two sat a line apart under labels that both said "attention". */
  unacknowledgedCount: number;
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

const SNOOZE_DURATIONS: { seconds: number; labelKey: string }[] = [
  { seconds: 3600, labelKey: "1h" },
  { seconds: 4 * 3600, labelKey: "4h" },
  { seconds: 24 * 3600, labelKey: "24h" },
];

function elapsedLabel(startedAt: number | null, nowSec: number): string {
  if (startedAt == null) return "—";
  // Timestamps are float epochs — floor so sub-minute ages never render
  // fractional seconds.
  const s = Math.max(0, Math.floor(nowSec - startedAt));
  return formatElapsed(s, { showSeconds: false });
}

export default function AttentionQueue({
  items,
  dischargedItems,
  unacknowledgedCount,
  nowSec,
}: Props) {
  const t = useTranslations("mission");
  const [showDischarged, setShowDischarged] = useState(false);

  const actionable = items.filter((i) => ACTIONABLE_REASONS.has(i.reason));
  // Informational digests, one row per cause. Orphaned runs are excluded
  // upstream (they never enter the attention list), so every row here is a
  // real failure or a stalled run — nothing pure-housekeeping.
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
              {unacknowledgedCount}
            </span>
          }
        >
          <span id="attention-heading">{t("attention.title")}</span>
        </SectionLabel>
        <div className="flex items-center gap-3">
          {dischargedItems.length > 0 && (
            <button
              type="button"
              onClick={() => setShowDischarged((v) => !v)}
              aria-expanded={showDischarged}
              className="font-data text-[length:var(--t-xs)] text-content-muted transition-colors duration-100 hover:text-content-primary"
            >
              {showDischarged
                ? t("attention.hideDischarged")
                : `${t("attention.showDischarged")} (${dischargedItems.length})`}
            </button>
          )}
          <Link
            to="/attention"
            className="font-data text-[length:var(--t-xs)] text-content-muted transition-colors duration-100"
          >
            {t("attention.viewAll")}
          </Link>
        </div>
      </div>

      <div className="overflow-hidden rounded border border-edge">
        {actionable.slice(0, MAX_ACTIONABLE_ROWS).map((item, idx) => (
          <AttentionRow key={item.id} item={item} nowSec={nowSec} first={idx === 0} />
        ))}
        {actionable.length > MAX_ACTIONABLE_ROWS && (
          <Link
            to="/attention"
            className="flex items-center justify-center bg-surface-raised px-3 py-2 font-data text-[length:var(--t-xs)] text-content-muted transition-colors duration-100"
            style={{ borderTop: "1px solid var(--edge-hairline)" }}
          >
            {t("attention.more", { count: actionable.length - MAX_ACTIONABLE_ROWS })}
          </Link>
        )}
        {/* Failed/stale are informational once read — one digest row each,
            count + latest + link into the full Attention tab. No raw rows
            underneath: a "collapse into one row" promise that still adds
            three more rows is not a collapse. */}
        {digests.map(({ reason, group }, idx) => (
          <DigestRow
            key={reason}
            reason={reason}
            group={group}
            nowSec={nowSec}
            first={idx === 0 && actionable.length === 0}
          />
        ))}
      </div>

      {showDischarged && (
        <div className="mt-2 overflow-hidden rounded border border-edge">
          {dischargedItems.length === 0 ? (
            <div className="bg-surface-raised px-3 py-2 font-data text-[length:var(--t-xs)] text-content-muted">
              {t("attention.dischargedEmpty")}
            </div>
          ) : (
            dischargedItems.map((item, idx) => (
              <AttentionRow key={item.id} item={item} nowSec={nowSec} first={idx === 0} />
            ))
          )}
        </div>
      )}
    </section>
  );
}

/** One line per reason: count + most recent item + age, linking into Fleet. */
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
      to="/attention"
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
  if (item.kind === "play") {
    return (
      <Link {...playDeepLink(item.sessionId)} className={className} style={style}>
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

const actionButtonClass =
  "shrink-0 rounded px-2 py-1 font-data text-[length:var(--t-xs)] font-semibold text-content-muted " +
  "transition-colors duration-100 hover:text-content-primary disabled:opacity-50";

/** Acknowledge/Resolve/Snooze/Expected/Undo — persists via PUT/DELETE, no
 * optimistic row removal: the row only changes once the next poll (≤3s)
 * confirms the write landed. */
function DispositionControls({ item }: { item: AttentionItem }) {
  const t = useTranslations("mission");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expectedOpen, setExpectedOpen] = useState(false);
  const [note, setNote] = useState("");
  const [durationSec, setDurationSec] = useState(SNOOZE_DURATIONS[0].seconds);

  async function save(
    state: AttentionDispositionState,
    extra?: { note?: string; expiresAt?: number },
  ) {
    setPending(true);
    setError(null);
    try {
      await putAttentionDisposition(item.id, {
        state,
        sourceStatus: item.status,
        note: extra?.note,
        expiresAt: extra?.expiresAt,
        revision: item.disposition?.revision,
      });
      setExpectedOpen(false);
      setNote("");
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : t("attention.action.saveFailed", { message: "" }),
      );
    } finally {
      setPending(false);
    }
  }

  async function undo() {
    setPending(true);
    setError(null);
    try {
      // The reducer joins a disposition stored under the pre-session-identity
      // key onto the current id, so clearing only the current one leaves the
      // legacy row to be re-joined on the next poll and the undo reads as a
      // no-op. Deleting an absent key is a no-op server-side.
      await Promise.all([
        deleteAttentionDisposition(item.id),
        ...(item.legacyId ? [deleteAttentionDisposition(item.legacyId)] : []),
      ]);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : t("attention.action.saveFailed", { message: "" }),
      );
    } finally {
      setPending(false);
    }
  }

  function submitExpected(e: FormEvent) {
    e.preventDefault();
    if (!note.trim()) {
      setError(t("attention.action.noteRequired"));
      return;
    }
    void save("expected", {
      note: note.trim(),
      expiresAt: Math.floor(Date.now() / 1000) + durationSec,
    });
  }

  const disposition = item.disposition;

  if (disposition) {
    return (
      <div className="flex shrink-0 items-center gap-2">
        <span className="font-data text-[length:var(--t-xs)] text-content-muted">
          {t(`attention.disposition.${disposition.state}` as Parameters<typeof t>[0])}
        </span>
        <button
          type="button"
          disabled={pending}
          aria-label={t("attention.action.undoAria", { name: item.name })}
          className={actionButtonClass}
          onClick={() => void undo()}
        >
          {t("attention.action.undo")}
        </button>
        {error && (
          <span className="text-[length:var(--t-xs)]" style={{ color: "var(--status-failure)" }}>
            {error}
          </span>
        )}
      </div>
    );
  }

  if (expectedOpen) {
    return (
      <form
        onSubmit={submitExpected}
        className="flex shrink-0 items-center gap-1.5"
        aria-label={t("attention.action.expectedAria", { name: item.name })}
      >
        <input
          type="text"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder={t("attention.action.notePlaceholder")}
          aria-label={t("attention.action.notePlaceholder")}
          className="w-32 rounded border border-edge bg-surface-base px-1.5 py-0.5 font-data text-[length:var(--t-xs)]"
        />
        <select
          value={durationSec}
          onChange={(e) => setDurationSec(Number(e.target.value))}
          aria-label={t("attention.action.expiryLabel")}
          className="rounded border border-edge bg-surface-base px-1 py-0.5 font-data text-[length:var(--t-xs)]"
        >
          {SNOOZE_DURATIONS.map((d) => (
            <option key={d.seconds} value={d.seconds}>
              {d.labelKey}
            </option>
          ))}
        </select>
        <button type="submit" disabled={pending} className={actionButtonClass}>
          {t("attention.action.confirm")}
        </button>
        <button
          type="button"
          disabled={pending}
          className={actionButtonClass}
          onClick={() => {
            setExpectedOpen(false);
            setError(null);
          }}
        >
          {t("attention.action.cancel")}
        </button>
        {error && (
          <span className="text-[length:var(--t-xs)]" style={{ color: "var(--status-failure)" }}>
            {error}
          </span>
        )}
      </form>
    );
  }

  // A gate is resolved by an actual approve/reject action on the run, not
  // by discharging it here — only Acknowledge ("seen, not fixed") applies.
  const isGated = item.reason === "gated";

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-1.5">
      <button
        type="button"
        disabled={pending}
        aria-label={t("attention.action.acknowledgeAria", { name: item.name })}
        className={actionButtonClass}
        onClick={() => void save("acknowledged")}
      >
        {t("attention.action.acknowledge")}
      </button>
      {!isGated && (
        <>
          <button
            type="button"
            disabled={pending}
            aria-label={t("attention.action.resolveAria", { name: item.name })}
            className={actionButtonClass}
            onClick={() => void save("resolved")}
          >
            {t("attention.action.resolve")}
          </button>
          <button
            type="button"
            disabled={pending}
            aria-label={t("attention.action.snoozeAria", { name: item.name, duration: "1h" })}
            className={actionButtonClass}
            onClick={() =>
              void save("snoozed", {
                expiresAt: Math.floor(Date.now() / 1000) + SNOOZE_DURATIONS[0].seconds,
              })
            }
          >
            {t("attention.action.snooze", { duration: SNOOZE_DURATIONS[0].labelKey })}
          </button>
          <button
            type="button"
            disabled={pending}
            aria-label={t("attention.action.expectedAria", { name: item.name })}
            className={actionButtonClass}
            onClick={() => setExpectedOpen(true)}
          >
            {t("attention.action.expected")}
          </button>
        </>
      )}
      {error && (
        <span className="text-[length:var(--t-xs)]" style={{ color: "var(--status-failure)" }}>
          {error}
        </span>
      )}
    </div>
  );
}

/** Exported for reuse on the dedicated Attention page — same row, same discharge controls. */
export function AttentionRow({
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
  const acknowledged = item.disposition?.state === "acknowledged";
  return (
    <div
      className="flex flex-wrap items-center gap-3 bg-surface-raised px-3 py-2 transition-colors duration-100"
      style={{
        borderTop: first ? undefined : "1px solid var(--edge-hairline)",
        opacity: acknowledged || item.disposition ? 0.7 : 1,
      }}
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

      <DispositionControls item={item} />

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
