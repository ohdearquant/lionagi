/**
 * Mission Control — home space.
 *
 * Attention leads when something needs a human; otherwise the living
 * system does: running work first, then recent history. The attention
 * digest is compact (never a wall of rows). All numbers tick in real
 * time. No manual refresh. No page reload.
 */

import { useTranslations } from "use-intl";
import StaleBadge from "./StaleBadge";
import AttentionQueue, { AttentionQueueSkeleton } from "./AttentionQueue";
import LiveBoard, { LiveBoardSkeleton } from "./LiveBoard";
import RecentRuns, { RecentRunsSkeleton } from "./RecentRuns";
import Pulse, { PulseSkeleton } from "./Pulse";
import ZeroState from "./ZeroState";
import Skeleton from "@/components/ui/Skeleton";
import { useLiveBoard } from "./useLiveBoard";
import { attentionNeedsHumanCount } from "./boardReducer";

export default function MissionControl() {
  const t = useTranslations("mission");
  const board = useLiveBoard();
  const runningCount = board.activeRuns.length + board.activeInvocations.length;
  // Orphaned rows (daemon-restart housekeeping) are visible in the digest
  // but carry no human action — they must not inflate "need attention".
  const attentionCount = attentionNeedsHumanCount(board.attentionItems);
  // Skeletons are for the FIRST fetch only. dataState leaves "loading" for
  // good on the first DATA_OK/DATA_ERROR, so later polls (including a
  // background refresh failure) never re-trigger this branch.
  const isInitialLoad = board.dataState === "loading";

  return (
    <div
      className="flex w-full flex-col gap-5 px-6 py-5"
      aria-label={t("page.ariaLabel")}
      aria-busy={isInitialLoad}
    >
      {/* Page heading row with glanceable summary */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[length:var(--t-base)] font-semibold text-content-primary">
            {t("page.title")}
          </h1>
          {isInitialLoad ? (
            <Skeleton className="mt-1.5 h-3 w-40" />
          ) : (
            <p className="mt-0.5 font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
              {t("page.summary", {
                running: runningCount,
                attention: attentionCount,
              })}
            </p>
          )}
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
       * Attention-first vertical stack:
       *   NEEDS ATTENTION full-width at the top, only when non-empty —
       *   when the system is clean the living board leads, no permanent
       *   "all clear" band spending prime real estate on a null.
       *   Then RUNNING NOW as a full-width strip, then RECENT RUNS.
       */}
      {/* Total-empty daemon: guided cards replace the board — any work
          exists → the real board below. Before the first successful fetch,
          skeletons stand in for all three shapes so nothing pops in at once. */}
      {isInitialLoad ? (
        <>
          <AttentionQueueSkeleton />
          <hr className="border-t border-edge" style={{ border: "none", borderTopWidth: "1px" }} />
          <LiveBoardSkeleton />
          <hr className="border-t border-edge" style={{ border: "none", borderTopWidth: "1px" }} />
          <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
            <PulseSkeleton />
            <RecentRunsSkeleton />
          </div>
        </>
      ) : board.systemEmpty ? (
        <ZeroState />
      ) : (
        <>
          {board.attentionItems.length > 0 && (
            <>
              <AttentionQueue
                items={board.attentionItems}
                nowSec={board.nowSec}
                dataState={board.dataState}
              />
              <hr
                className="border-t border-edge"
                style={{ border: "none", borderTopWidth: "1px" }}
              />
            </>
          )}

          <LiveBoard
            activeRuns={board.activeRuns}
            activeInvocations={board.activeInvocations}
            nowSec={board.nowSec}
          />

          <hr className="border-t border-edge" style={{ border: "none", borderTopWidth: "1px" }} />

          {/* Pulse + Recent share a row on wide screens; stack below 1280px. */}
          <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
            <Pulse />
            <RecentRuns runs={board.recentRuns} nowSec={board.nowSec} />
          </div>
        </>
      )}
    </div>
  );
}
