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
import AttentionQueue from "./AttentionQueue";
import LiveBoard from "./LiveBoard";
import RecentRuns from "./RecentRuns";
import Pulse from "./Pulse";
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
       * Attention-first vertical stack:
       *   NEEDS ATTENTION full-width at the top, only when non-empty —
       *   when the system is clean the living board leads, no permanent
       *   "all clear" band spending prime real estate on a null.
       *   Then RUNNING NOW as a full-width strip, then RECENT RUNS.
       */}
      {board.attentionItems.length > 0 && (
        <>
          <AttentionQueue
            items={board.attentionItems}
            nowSec={board.nowSec}
            dataState={board.dataState}
          />
          <hr className="border-t border-edge" style={{ border: "none", borderTopWidth: "1px" }} />
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
    </div>
  );
}
