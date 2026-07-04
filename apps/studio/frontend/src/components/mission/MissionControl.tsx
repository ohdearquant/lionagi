/**
 * Mission Control — home space.
 *
 * The living system leads: running work first, then a compact attention
 * digest (never a wall of rows), then recent history. All numbers tick in
 * real time. No manual refresh. No page reload.
 */

import { useTranslations } from "use-intl";
import StaleBadge from "./StaleBadge";
import AttentionQueue from "./AttentionQueue";
import LiveBoard from "./LiveBoard";
import RecentRuns from "./RecentRuns";
import { useLiveBoard } from "./useLiveBoard";

export default function MissionControl() {
  const t = useTranslations("mission");
  const board = useLiveBoard();
  const runningCount = board.activeRuns.length + board.activeInvocations.length;

  return (
    <div className="flex w-full flex-col gap-5 px-6 py-5" aria-label={t("page.ariaLabel")}>
      {/* Page heading row with glanceable summary */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[length:var(--t-base)] font-semibold text-content-primary">
            {t("page.title")}
          </h1>
          <p className="mt-0.5 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
            {t("page.summary", {
              running: runningCount,
              attention: board.attentionItems.length,
            })}
          </p>
        </div>
        <StaleBadge
          dataState={board.dataState}
          lastUpdatedMs={board.lastUpdatedMs}
          errorMessage={board.errorMessage}
        />
      </div>

      {/* Hairline separator */}
      <hr className="border-t border-edge" style={{ border: "none", borderTopWidth: "1px" }} />

      {/*
       * Two-zone layout at ≥1280px:
       *   left (flex 3): RUNNING NOW + NEEDS ATTENTION stacked
       *   right (flex 2): RECENT RUNS column, full height
       * Below 1280px: single-column vertical stack.
       */}
      <div className="flex flex-col gap-5 xl:grid xl:grid-cols-[3fr_2fr] xl:gap-6 xl:items-start">
        {/* Left zone: live board + attention queue */}
        <div className="flex flex-col gap-5">
          <LiveBoard
            activeRuns={board.activeRuns}
            activeInvocations={board.activeInvocations}
            nowSec={board.nowSec}
          />

          {board.attentionItems.length > 0 && (
            <>
              <hr
                className="border-t border-edge"
                style={{ border: "none", borderTopWidth: "1px" }}
              />
              <AttentionQueue
                items={board.attentionItems}
                nowSec={board.nowSec}
                dataState={board.dataState}
              />
            </>
          )}
        </div>

        {/* Right zone: recent terminal runs — hairline divider on wide layouts */}
        <div className="xl:border-l xl:border-edge xl:pl-6">
          {/* Hairline separator only on narrow (vertical) layouts */}
          <hr
            className="xl:hidden border-t border-edge"
            style={{ border: "none", borderTopWidth: "1px" }}
          />
          <RecentRuns runs={board.recentRuns} nowSec={board.nowSec} />
        </div>
      </div>
    </div>
  );
}
