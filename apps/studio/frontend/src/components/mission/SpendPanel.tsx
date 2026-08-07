/**
 * SpendPanel — reported-spend aggregate beside Pulse on the home surface.
 *
 * Shares Pulse's window selection (no selector of its own) so the two cards
 * never describe a different population for the same stated window.
 * Coverage — the fraction of the window's runs that reported a cost at
 * all — renders at the same visual weight as the dollar figure, never a
 * subordinate footnote: a window where most runs are unreported is not an
 * edge case worth hiding in small print.
 */

import { useTranslations } from "use-intl";
import SectionLabel from "@/components/ui/SectionLabel";
import Skeleton from "@/components/ui/Skeleton";
import { useSpendPanel } from "./useSpendPanel";
import { formatCostLowerBound, formatCostUsd } from "@/lib/usageFormat";
import type { ActivityWindow } from "@/lib/api";

export function SpendPanelSkeleton() {
  return (
    <div aria-hidden="true">
      <div className="mb-2 flex items-center justify-between">
        <Skeleton className="h-4 w-24" />
      </div>
      <div className="rounded border border-edge bg-surface-raised px-4 py-3">
        <Skeleton className="h-7 w-28" />
        <div className="mt-2 flex items-center justify-between">
          <Skeleton className="h-3 w-28" />
        </div>
      </div>
    </div>
  );
}

interface Props {
  window: ActivityWindow;
}

export default function SpendPanel({ window: window_ }: Props) {
  const t = useTranslations("mission");
  const { data, error, loading } = useSpendPanel(window_);
  const errorMessage = error === null ? null : error || t("spend.unreachable");

  // Same non-negotiable contract as the run-list cost column: a reported sum
  // that excludes unreported rows renders as a lower bound, never a bare
  // number that could be mistaken for the whole window's spend.
  const headline =
    data == null
      ? null
      : data.reported_count === 0
        ? formatCostUsd(null)
        : data.unreported_count > 0
          ? formatCostLowerBound(data.reported_usd)
          : formatCostUsd(data.reported_usd);

  return (
    <section aria-labelledby="spend-heading">
      <div className="mb-2 flex items-center justify-between">
        <SectionLabel>
          <span id="spend-heading">{t("spend.title")}</span>
        </SectionLabel>
      </div>

      <div className="rounded border border-edge bg-surface-raised px-4 py-3">
        {loading ? (
          <p className="text-[length:var(--t-sm)] text-content-muted">{t("spend.loading")}</p>
        ) : data === null ? (
          <p className="text-[length:var(--t-sm)] text-content-muted">
            {t("spend.error", { message: errorMessage ?? "" })}
          </p>
        ) : data.total_count === 0 ? (
          <p className="text-[length:var(--t-sm)] text-content-muted">{t("spend.empty")}</p>
        ) : (
          <>
            <div className="flex items-baseline justify-between font-data tabular-nums">
              <span className="text-[length:var(--t-lg)] font-semibold text-content-primary">
                {headline}
              </span>
              {/* Coverage sits at the same visual weight as the dollar figure —
                  same row, same font family/size class, not a smaller caption. */}
              <span className="text-[length:var(--t-sm)] text-content-secondary">
                {t("spend.coverage", {
                  reported: data.reported_count,
                  total: data.total_count,
                })}
              </span>
            </div>
            {error !== null && (
              <p className="mt-1 text-[length:var(--t-xs)] text-content-muted">
                {t("spend.staleHint")}
              </p>
            )}
          </>
        )}
      </div>
    </section>
  );
}
