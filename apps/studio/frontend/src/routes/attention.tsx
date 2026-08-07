/**
 * Attention — the full queue, not the compact Overview digest.
 *
 * Overview shows actionable rows plus one collapsed digest per informational
 * reason (never a wall of red). This page is where that "view all" link
 * lands: every item, active or discharged, filterable, using the same rows
 * and discharge controls as the Overview digest so behavior never diverges
 * between the two surfaces.
 */
import { createFileRoute } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { useTranslations } from "use-intl";
import TabBar from "@/components/shell/TabBar";
import StaleBadge from "@/components/mission/StaleBadge";
import { AttentionRow } from "@/components/mission/AttentionQueue";
import EmptyState from "@/components/ui/EmptyState";
import Skeleton from "@/components/ui/Skeleton";
import { useLiveBoard } from "@/components/mission/useLiveBoard";
import type { AttentionItem } from "@/components/mission/boardReducer";

export const Route = createFileRoute("/attention")({
  component: AttentionPage,
});

type Filter = "active" | "discharged" | "all";
const FILTERS: Filter[] = ["active", "discharged", "all"];

export function itemsForFilter(
  filter: Filter,
  active: AttentionItem[],
  discharged: AttentionItem[],
): AttentionItem[] {
  if (filter === "active") return active;
  if (filter === "discharged") return discharged;
  return [...active, ...discharged];
}

function AttentionPage() {
  const tShell = useTranslations("shell");
  const t = useTranslations("mission");
  const board = useLiveBoard();
  const [filter, setFilter] = useState<Filter>("active");

  const items = useMemo(
    () => itemsForFilter(filter, board.attentionItems, board.dischargedAttentionItems),
    [filter, board.attentionItems, board.dischargedAttentionItems],
  );

  const isInitialLoad = board.dataState === "loading";

  return (
    <div className="flex h-full w-full flex-col">
      <div className="px-6 pt-4">
        <TabBar
          ariaLabel={tShell("tabs.homeAria")}
          tabs={[
            { id: "overview", label: tShell("tabs.overview"), to: "/", active: false },
            { id: "fleet", label: tShell("tabs.fleet"), to: "/fleet", active: false },
            { id: "attention", label: tShell("tabs.attention"), to: "/attention", active: true },
          ]}
        />
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-4 px-6 py-5">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-page-title font-semibold text-content-primary">
              {t("attention.page.title")}
            </h1>
            <p className="mt-0.5 text-body text-content-muted">{t("attention.page.subtitle")}</p>
          </div>
          <StaleBadge
            dataState={board.dataState}
            lastUpdatedMs={board.lastUpdatedMs}
            errorMessage={board.errorMessage}
          />
        </div>

        <div className="flex gap-1.5" role="tablist" aria-label={t("attention.page.title")}>
          {FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              role="tab"
              aria-selected={filter === f}
              onClick={() => setFilter(f)}
              className={`rounded px-2.5 py-1 font-data text-[length:var(--t-xs)] font-semibold transition-colors duration-100 ${
                filter === f ? "text-accent" : "text-content-muted hover:text-content-primary"
              }`}
              style={
                filter === f
                  ? { background: "color-mix(in srgb, var(--accent) 15%, transparent)" }
                  : undefined
              }
            >
              {t(
                `attention.page.filter${f.charAt(0).toUpperCase()}${f.slice(1)}` as Parameters<
                  typeof t
                >[0],
              )}
            </button>
          ))}
        </div>

        {isInitialLoad ? (
          <div className="space-y-2">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-10 rounded" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            glyph="✓"
            title={t(
              `attention.page.empty${filter.charAt(0).toUpperCase()}${filter.slice(1)}` as Parameters<
                typeof t
              >[0],
            )}
            className="pb-16"
          />
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto rounded border border-edge">
            {items.map((item, idx) => (
              <AttentionRow key={item.id} item={item} nowSec={board.nowSec} first={idx === 0} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
