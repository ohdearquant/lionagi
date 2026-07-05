export type FilterChipTone = "blue" | "amber" | "green" | "neutral";

export interface FilterChipProps {
  label: string;
  /** Optional numeric count displayed after the label. */
  count?: number;
  active: boolean;
  tone?: FilterChipTone;
  onToggle: () => void;
  className?: string;
}

const ACTIVE_TONE_CLASS: Record<FilterChipTone, string> = {
  blue: "border-status-running/40 bg-status-running-bg text-status-running",
  amber: "border-status-warning/40 bg-status-warning-bg text-status-warning",
  green: "border-status-success/40 bg-status-success-bg text-status-success",
  neutral: "border-edge-strong bg-surface-overlay text-content-secondary",
};

const INACTIVE_CLASS = "border-edge text-content-muted";

/**
 * Toggle chip used in filter bars. Renders a colored active state per `tone`
 * and a neutral inactive state. Pairs with FilterChipBar for layout.
 */
export default function FilterChip({
  label,
  count,
  active,
  tone = "neutral",
  onToggle,
  className,
}: FilterChipProps) {
  const toneClass = active ? ACTIVE_TONE_CLASS[tone] : INACTIVE_CLASS;
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={active}
      className={[
        "rounded border px-1.5 py-0 text-[length:var(--t-xs)] font-medium uppercase tracking-wide transition-colors hover:brightness-110",
        toneClass,
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {label}
      {count != null && <> {count}</>}
    </button>
  );
}
