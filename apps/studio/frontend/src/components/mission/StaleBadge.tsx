/**
 * Stale-data badge. Appears only when dataState is "stale" or "error".
 * Three distinct visual states (empty/stale/error) are never conflatable.
 *
 * Driven by a client-side watchdog timer (REDESIGN-EXEC addendum):
 * - stale: pipeline silent — shows "last updated Xs ago"
 * - error: known failure — shows error detail
 */

import { useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import { IconClose } from "@/components/ui/icons";
import StatusDot from "@/components/ui/StatusDot";
import type { DataState } from "./boardReducer";

interface Props {
  dataState: DataState;
  lastUpdatedMs: number | null;
  errorMessage: string | null;
}

function secondsAgo(lastUpdatedMs: number | null): number {
  if (lastUpdatedMs == null) return 0;
  return Math.floor((Date.now() - lastUpdatedMs) / 1000);
}

export default function StaleBadge({ dataState, lastUpdatedMs, errorMessage }: Props) {
  const t = useTranslations("mission");
  const [age, setAge] = useState(() => secondsAgo(lastUpdatedMs));

  useEffect(() => {
    if (dataState !== "stale") return;
    const t = setInterval(() => setAge(secondsAgo(lastUpdatedMs)), 1_000);
    return () => clearInterval(t);
  }, [dataState, lastUpdatedMs]);

  if (dataState !== "stale" && dataState !== "error") return null;

  if (dataState === "error") {
    return (
      <div
        role="status"
        aria-live="polite"
        className="flex items-center gap-2 rounded border px-3 py-1.5 font-data text-[length:var(--t-xs)] font-medium"
        style={{
          borderColor: "var(--status-failure)",
          background: "color-mix(in srgb, var(--status-failure) 8%, transparent)",
          color: "var(--status-failure)",
        }}
      >
        <span aria-hidden="true" className="flex items-center">
          <IconClose size={10} strokeWidth={2.5} />
        </span>
        <span>{t("stale.error", { message: errorMessage ?? t("stale.unreachable") })}</span>
      </div>
    );
  }

  // stale — ambient status, not an alert: a quiet timestamp in muted type, with
  // the shared dot carrying the state. The warning treatment is reserved for
  // the error branch above, where something is actually wrong.
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-center gap-1.5 font-data text-[length:var(--t-xs)] text-content-muted"
    >
      <StatusDot status="stale" />
      <span>{t("stale.lastUpdated", { age })}</span>
    </div>
  );
}
