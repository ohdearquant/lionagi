/**
 * Pulse — windowed activity aggregate for the home surface.
 *
 * Stacked-bar sparkline over dense server buckets (24 hourly / 7 daily),
 * plus completion rate and total. Inline SVG, no chart dependency.
 * Cost/token cells stay hidden until the daemon exposes those fields.
 */

import { useState } from "react";
import { useTranslations } from "use-intl";
import SectionLabel from "@/components/ui/SectionLabel";
import Skeleton from "@/components/ui/Skeleton";
import { usePulse } from "./usePulse";
import { CHART_H, chartWidth, sparklineRects } from "./sparkline";
import type { SparkRect } from "./sparkline";
import type { ActivityBucket, ActivityWindow } from "@/lib/api";

const SEGMENT_COLOR: Record<SparkRect["kind"], string> = {
  completed: "var(--status-success)",
  failed: "var(--status-failure)",
  cancelled: "var(--edge-strong, var(--edge))",
  running: "var(--status-running)",
  stub: "var(--edge-hairline, var(--edge))",
};

function Sparkline({ buckets }: { buckets: ActivityBucket[] }) {
  const rects = sparklineRects(buckets);
  return (
    <svg
      viewBox={`0 0 ${chartWidth(buckets.length)} ${CHART_H}`}
      preserveAspectRatio="none"
      className="h-10 w-full"
      aria-hidden="true"
    >
      {rects.map((r, i) => (
        <rect
          key={i}
          x={r.x}
          y={r.y}
          width={r.width}
          height={r.height}
          fill={SEGMENT_COLOR[r.kind]}
        />
      ))}
    </svg>
  );
}

/** Shimmering placeholder matching the sparkline + stats card. */
export function PulseSkeleton() {
  return (
    <div aria-hidden="true">
      <div className="mb-2 flex items-center justify-between">
        <Skeleton className="h-4 w-16" />
        <Skeleton className="h-5 w-20 rounded" />
      </div>
      <div className="rounded border border-edge bg-surface-raised px-4 py-3">
        <Skeleton className="h-10 w-full" />
        <div className="mt-2 flex items-center justify-between">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-3 w-14" />
        </div>
      </div>
    </div>
  );
}

export default function Pulse() {
  const t = useTranslations("mission");
  const [window_, setWindow] = useState<ActivityWindow>("24h");
  const { data, error, loading } = usePulse(window_);

  const ratePct = data?.completion_rate != null ? Math.round(data.completion_rate * 100) : null;
  // "" marks a failure without a usable message — localize the fallback.
  const errorMessage = error === null ? null : error || t("pulse.unreachable");

  return (
    <section aria-labelledby="pulse-heading">
      <div className="mb-2 flex items-center justify-between">
        <SectionLabel
          trailing={
            <span role="group" aria-label={t("pulse.windowLabel")} className="flex gap-1">
              {(["24h", "7d"] as const).map((w) => (
                <button
                  key={w}
                  type="button"
                  onClick={() => setWindow(w)}
                  aria-pressed={window_ === w}
                  className={`rounded px-1.5 py-0.5 font-data text-[length:var(--t-xs)] transition-colors duration-100 ${
                    window_ === w
                      ? "bg-surface-overlay text-content-primary"
                      : "text-content-muted hover:text-content-secondary"
                  }`}
                >
                  {t(`pulse.window.${w}`)}
                </button>
              ))}
            </span>
          }
        >
          <span id="pulse-heading">{t("pulse.title")}</span>
        </SectionLabel>
      </div>

      <div className="rounded border border-edge bg-surface-raised px-4 py-3">
        {loading ? (
          <p className="text-[length:var(--t-sm)] text-content-muted">{t("pulse.loading")}</p>
        ) : data === null ? (
          <p className="text-[length:var(--t-sm)] text-content-muted">
            {t("pulse.error", { message: errorMessage ?? "" })}
          </p>
        ) : (
          <>
            <Sparkline buckets={data.buckets} />
            <div className="mt-2 flex items-baseline justify-between font-data tabular-nums">
              <span className="text-[length:var(--t-sm)] text-content-secondary">
                {ratePct != null
                  ? t("pulse.completionRate", { rate: ratePct })
                  : t("pulse.noTerminalRuns")}
              </span>
              <span className="text-[length:var(--t-xs)] text-content-muted">
                {t("pulse.total", { count: data.total })}
              </span>
            </div>
            {error !== null && (
              <p className="mt-1 text-[length:var(--t-xs)] text-content-muted">
                {t("pulse.staleHint")}
              </p>
            )}
          </>
        )}
      </div>
    </section>
  );
}
