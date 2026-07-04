"use client";

import Button from "./Button";

export type TimeRange = "1h" | "24h" | "7d" | "all";

export const TIME_RANGES: { value: TimeRange; label: string }[] = [
  { value: "1h", label: "1h" },
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "all", label: "All" },
];

export function rangeToSeconds(range: TimeRange): number | null {
  if (range === "all") return null;
  if (range === "1h") return 3600;
  if (range === "24h") return 86400;
  if (range === "7d") return 86400 * 7;
  return null;
}

export interface TimeRangeChipsProps {
  value: TimeRange;
  onChange: (value: TimeRange) => void;
  className?: string;
}

export default function TimeRangeChips({ value, onChange, className }: TimeRangeChipsProps) {
  return (
    <div
      role="radiogroup"
      aria-label="Time range"
      className={[
        "inline-flex items-center gap-0.5 rounded border border-edge bg-surface-overlay p-0.5",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {TIME_RANGES.map((r) => (
        <Button
          key={r.value}
          size="sm"
          variant={value === r.value ? "primary" : "ghost"}
          onClick={() => onChange(r.value)}
          aria-checked={value === r.value}
          role="radio"
          className={value === r.value ? "" : "border-transparent"}
        >
          {r.label}
        </Button>
      ))}
    </div>
  );
}
