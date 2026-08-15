import { useTranslations } from "use-intl";
import type { HookAttachment, HookEvent } from "@/lib/api";
import Button from "@/components/ui/Button";

const EVENTS: HookEvent[] = [
  "pre_tool",
  "post_tool",
  "prompt_submit",
  "post_response",
  "session_start",
  "session_end",
];

interface Props {
  attachments: HookAttachment[];
  onChange: (next: HookAttachment[]) => void;
  /** Names available in the hook library — the hook dropdown's options. */
  hookNames: string[];
  disabled?: boolean;
}

/**
 * Assembly rows binding named library hooks to provider-neutral events.
 * Shared between the Operator's assembly and each agent profile's hooks
 * section — the assembly is the same shape wherever it is consumed.
 */
export default function HookAssemblyEditor({ attachments, onChange, hookNames, disabled }: Props) {
  const t = useTranslations("library.hooks");

  const update = (index: number, patch: Partial<HookAttachment>) => {
    const next = attachments.map((row, i) => (i === index ? { ...row, ...patch } : row));
    // An emptied matcher is removed, not stored as "".
    if (patch.matcher !== undefined && !patch.matcher) {
      delete next[index].matcher;
    }
    onChange(next);
  };

  return (
    <div className="flex flex-col gap-2">
      {attachments.length === 0 && (
        <p className="text-[length:var(--t-sm)] text-content-muted">{t("noAttachments")}</p>
      )}
      {attachments.map((row, index) => (
        <div key={index} className="flex flex-wrap items-center gap-2">
          <select
            value={row.hook}
            onChange={(e) => update(index, { hook: e.target.value })}
            disabled={disabled}
            aria-label={t("attachmentHook")}
            className="rounded border border-edge bg-surface-overlay px-2 py-1 font-data text-[length:var(--t-sm)] text-content-primary"
          >
            {/* Keep a dangling name visible instead of silently snapping to
                another hook — the save surfaces the resolution error. */}
            {!hookNames.includes(row.hook) && <option value={row.hook}>{row.hook}</option>}
            {hookNames.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <span className="text-[length:var(--t-xs)] text-content-muted">{t("attachmentOn")}</span>
          <select
            value={row.event}
            onChange={(e) => update(index, { event: e.target.value as HookEvent })}
            disabled={disabled}
            aria-label={t("attachmentEvent")}
            className="rounded border border-edge bg-surface-overlay px-2 py-1 font-data text-[length:var(--t-sm)] text-content-primary"
          >
            {EVENTS.map((event) => (
              <option key={event} value={event}>
                {event}
              </option>
            ))}
          </select>
          <input
            type="text"
            value={row.matcher ?? ""}
            onChange={(e) => update(index, { matcher: e.target.value })}
            placeholder={t("matcherPlaceholder")}
            disabled={disabled}
            aria-label={t("attachmentMatcher")}
            className="w-32 rounded border border-edge bg-surface-overlay px-2 py-1 font-data text-[length:var(--t-sm)] text-content-primary"
          />
          <Button
            size="sm"
            variant="ghost"
            disabled={disabled}
            onClick={() => onChange(attachments.filter((_, i) => i !== index))}
            aria-label={t("removeAttachment")}
          >
            ✕
          </Button>
        </div>
      ))}
      <Button
        size="sm"
        variant="secondary"
        disabled={disabled || hookNames.length === 0}
        onClick={() => onChange([...attachments, { hook: hookNames[0] ?? "", event: "pre_tool" }])}
        className="w-fit"
      >
        + {t("addAttachment")}
      </Button>
      {hookNames.length === 0 && (
        <p className="text-[length:var(--t-xs)] text-content-muted">{t("emptyLibraryHint")}</p>
      )}
    </div>
  );
}
