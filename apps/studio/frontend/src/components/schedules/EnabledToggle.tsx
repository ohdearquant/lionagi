import { useState } from "react";
import { useTranslations } from "use-intl";
import { disableSchedule, enableSchedule } from "@/lib/api";

export default function EnabledToggle({
  scheduleId,
  enabled,
  onToggled,
}: {
  scheduleId: string;
  enabled: boolean;
  onToggled: () => void;
}) {
  const t = useTranslations("schedules.card");
  const [busy, setBusy] = useState(false);

  async function handleClick(e: React.MouseEvent) {
    e.stopPropagation();
    setBusy(true);
    try {
      if (enabled) {
        await disableSchedule(scheduleId);
      } else {
        await enableSchedule(scheduleId);
      }
      onToggled();
    } catch {
      // silent — UI stays consistent until next reload
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      type="button"
      onClick={(e) => void handleClick(e)}
      disabled={busy}
      aria-label={enabled ? t("disable") : t("enable")}
      aria-pressed={enabled}
      title={enabled ? t("disable") : t("enable")}
      className={[
        "relative inline-flex h-4 w-7 shrink-0 items-center rounded-full border transition-colors duration-150 focus:outline-none focus:ring-2 focus:ring-interactive-primary focus:ring-offset-1 focus:ring-offset-surface-base",
        enabled ? "border-status-success/50 bg-status-success" : "border-edge bg-surface-overlay",
        busy ? "opacity-60 cursor-not-allowed" : "cursor-pointer",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span
        className={[
          "inline-block h-2.5 w-2.5 rounded-full bg-white shadow transition-transform duration-150",
          enabled ? "translate-x-3" : "translate-x-0.5",
        ].join(" ")}
      />
    </button>
  );
}
