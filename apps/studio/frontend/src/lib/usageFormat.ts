/**
 * Shared cost/token formatting for the cost-visibility surfaces (spend
 * panel, run-list cost column, per-dimension rollups).
 *
 * The contract this module exists to enforce: `null`/`undefined` means the
 * provider never reported a cost for that record (unknown) and renders as
 * an em dash; a genuine `0` is a real, distinct value and renders as
 * `$0.00`. Callers must never coerce one into the other before calling in.
 */

const UNREPORTED = "—";

export function formatCostUsd(cost: number | null | undefined): string {
  if (cost == null) return UNREPORTED;
  // Sub-cent values (e.g. a single cheap call) round to $0.00 at 2 decimals,
  // which reads as "free" — 4 decimals only in that narrow band keeps a
  // genuine $0.00 (2 decimals) visually distinct from "too small to show".
  const decimals = cost > 0 && cost < 0.01 ? 4 : 2;
  return `$${cost.toFixed(decimals)}`;
}

export function formatTokenCount(count: number | null | undefined): string {
  if (count == null) return UNREPORTED;
  if (count < 1000) return String(count);
  if (count < 1_000_000) return `${(count / 1000).toFixed(count < 10_000 ? 1 : 0)}k`;
  return `${(count / 1_000_000).toFixed(1)}m`;
}

/** `≥ $X` lower-bound form for a reported sum that excludes unreported rows. */
export function formatCostLowerBound(cost: number | null | undefined): string {
  if (cost == null) return UNREPORTED;
  return `≥ ${formatCostUsd(cost)}`;
}
