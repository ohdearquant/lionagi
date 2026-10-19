import { useState } from "react";
import { createPortal } from "react-dom";
import { useTranslations } from "use-intl";
import { disableSchedule, enableSchedule } from "@/lib/api";
import Button from "@/components/ui/Button";
import { FieldLabel, TextArea } from "@/components/ui/Field";
import Modal from "@/components/ui/Modal";
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
  const tDetail = useTranslations("schedules.detail");
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);
  const [reasonOpen, setReasonOpen] = useState(false);
  const [reason, setReason] = useState("");

  async function handleClick(e: React.MouseEvent) {
    e.stopPropagation();
    if (enabled) {
      setReasonOpen(true);
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
    const trimmedReason = reason.trim();
    if (!trimmedReason) return;
    setBusy(true);
    try {
      await disableSchedule(scheduleId, { reason: trimmedReason });
      setReason("");
      setReasonOpen(false);
      onToggled();
    } catch {
      // Keep both the dialog and the authored reason intact so a transient
      // failure never makes the user reconstruct why they were pausing it.
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
      {reasonOpen &&
        createPortal(
          // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events -- contain portal events so they cannot open the schedule row behind it; Modal owns keyboard handling
          <div onClick={(event) => event.stopPropagation()}>
            <Modal
              title={t("disable")}
              closeLabel={tDetail("cancel")}
              onClose={() => {
                if (!busy) setReasonOpen(false);
              }}
              maxWidth="max-w-md"
            >
              <div className="flex flex-col gap-4 p-5">
                <FieldLabel label={t("disableReason")} required hint={t("disableReasonHint")}>
                  <TextArea
                    aria-label={t("disableReason")}
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    maxLength={500}
                    rows={3}
                    disabled={busy}
                  />
                </FieldLabel>
                <div className="flex justify-end gap-2">
                  <Button variant="ghost" onClick={() => setReasonOpen(false)} disabled={busy}>
                    {tDetail("cancel")}
                  </Button>
                  <Button
                    variant="danger"
                    onClick={() => void handleDisable()}
                    disabled={busy || !reason.trim()}
                  >
                    {t("disable")}
                  </Button>
                </div>
              </div>
            </Modal>
          </div>,
          document.body,
        )}
    </>
  );
}
