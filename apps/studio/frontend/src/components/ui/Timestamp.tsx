"use client";

import { useEffect, useState } from "react";

export interface TimestampProps {
  value: string | number | Date | null | undefined;
  // when true, render exact (e.g. "May 19, 2026, 11:19:36 PM") instead of relative
  exact?: boolean;
  className?: string;
}

function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value == null) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") {
    // seconds vs ms heuristic — values > 1e12 are ms, smaller are seconds
    const ms = value > 1e12 ? value : value * 1000;
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

function relative(date: Date, now: Date): string {
  const diffMs = now.getTime() - date.getTime();
  const sec = Math.abs(diffMs) / 1000;
  const suffix = diffMs >= 0 ? "ago" : "from now";
  if (sec < 5) return "just now";
  if (sec < 60) return `${Math.floor(sec)}s ${suffix}`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${suffix}`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ${suffix}`;
  if (sec < 86400 * 30) return `${Math.floor(sec / 86400)}d ${suffix}`;
  if (sec < 86400 * 365) return `${Math.floor(sec / (86400 * 30))}mo ${suffix}`;
  return `${Math.floor(sec / (86400 * 365))}y ${suffix}`;
}

function exactFormat(date: Date): string {
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

export default function Timestamp({ value, exact = false, className }: TimestampProps) {
  const date = toDate(value);
  const [now, setNow] = useState<Date | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- client-only clock init; null during SSR avoids hydration mismatch
    setNow(new Date());
    const id = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(id);
  }, []);

  if (!date) {
    return (
      <span className={["text-content-muted tabular-nums", className].filter(Boolean).join(" ")}>
        —
      </span>
    );
  }

  const exactText = exactFormat(date);
  const display = exact || !now ? exactText : relative(date, now);

  return (
    <time
      dateTime={date.toISOString()}
      title={exactText}
      className={["tabular-nums", className].filter(Boolean).join(" ")}
    >
      {display}
    </time>
  );
}
