export interface FormatElapsedOptions {
  /** Show leftover seconds inside the minute bucket, e.g. "1m 1s" vs "1m" (default: true). */
  showSeconds?: boolean;
  /** Roll hours over into days past the 24h mark, e.g. "1d 1h" vs "25h" (default: false). */
  capAtDays?: boolean;
  /** Keep one decimal for raw sub-minute spans, e.g. "59.9s" instead of flooring
   * to "59s" (default: false). Minute-and-up buckets are always floored either way. */
  subMinuteDecimal?: boolean;
}

/** Compact "Xh Ym"-style duration label shared by the run-age displays across
 * Mission and Fleet — previously reimplemented with small drifts in each. */
export function formatElapsed(
  seconds: number | null | undefined,
  opts: FormatElapsedOptions = {},
): string {
  const { showSeconds = true, capAtDays = false, subMinuteDecimal = false } = opts;
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";

  if (subMinuteDecimal && seconds < 60) {
    return `${Number(seconds.toFixed(1))}s`;
  }

  const total = Math.floor(seconds);
  if (total < 60) return `${total}s`;

  const m = Math.floor(total / 60);
  if (m < 60) {
    if (!showSeconds) return `${m}m`;
    const s = total % 60;
    return s > 0 ? `${m}m ${s}s` : `${m}m`;
  }

  const h = Math.floor(m / 60);
  if (capAtDays && h >= 24) {
    const d = Math.floor(h / 24);
    const hh = h - d * 24;
    return hh > 0 ? `${d}d ${hh}h` : `${d}d`;
  }
  const mm = m - h * 60;
  return mm > 0 ? `${h}h ${mm}m` : `${h}h`;
}
