import { useEffect, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import { disableSchedule, enableSchedule } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";

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
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);
  const [askingReason, setAskingReason] = useState(false);
  const [reason, setReason] = useState("");
  const reasonInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (askingReason) reasonInputRef.current?.focus();
  }, [askingReason]);

  async function handleClick(e: React.MouseEvent) {
    e.stopPropagation();
    if (enabled) {
      setAskingReason(true);
      return;
    }
    setBusy(true);
    try {
      await enableSchedule(scheduleId);
      onToggled();
    } catch {
      toast(t("toggleFailed"), "error");
    } finally {
      setBusy(false);
    }
  }

  async function handleDisable() {
    const summary = reason.trim();
    if (!summary) return;
    setBusy(true);
    try {
      await disableSchedule(scheduleId, summary);
      setAskingReason(false);
      setReason("");
      onToggled();
    } catch {
      toast(t("toggleFailed"), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
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
      {askingReason && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/50 p-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="disable-schedule-title"
            className="w-full max-w-md rounded-lg border border-edge bg-surface-raised p-5 shadow-card"
          >
            <h2
              id="disable-schedule-title"
              className="font-data text-label font-semibold text-content-primary"
            >
              {t("disable")}
            </h2>
            <p className="mt-2 text-meta text-content-secondary">{t("disableReasonHint")}</p>
            <label className="mt-4 block text-meta font-medium text-content-secondary">
              {t("disableReason")}
              <input
                ref={reasonInputRef}
                name="disable-reason"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                maxLength={1000}
                className="mt-1 w-full rounded border border-edge bg-surface-base px-3 py-2 text-body text-content-primary outline-none focus:border-interactive-primary"
              />
            </label>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => setAskingReason(false)}
                className="rounded px-3 py-1.5 text-meta text-content-secondary hover:bg-surface-overlay"
              >
                {t("cancelDisable")}
              </button>
              <button
                type="button"
                disabled={busy || !reason.trim()}
                onClick={() => void handleDisable()}
                className="rounded bg-status-error px-3 py-1.5 text-meta font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
              >
                {t("disable")}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
