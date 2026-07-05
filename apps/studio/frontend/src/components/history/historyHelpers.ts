const STATUS_COLORS: Record<string, string> = {
  running: "var(--status-running)",
  completed: "var(--status-success)",
  success: "var(--status-success)",
  failed: "var(--status-failure)",
  failure: "var(--status-failure)",
  cancelled: "var(--content-muted)",
  pending: "var(--status-pending)",
  queued: "var(--status-pending)",
};

const STATUS_GLYPHS: Record<string, string> = {
  running: "◉",
  completed: "✓",
  success: "✓",
  failed: "✗",
  failure: "✗",
  cancelled: "○",
  pending: "◌",
  queued: "◌",
};

export function statusColor(s: string): string {
  return STATUS_COLORS[s.toLowerCase()] ?? "var(--content-muted)";
}

export function statusGlyph(s: string): string {
  return STATUS_GLYPHS[s.toLowerCase()] ?? "·";
}

export function formatDuration(startEpochSec: number, endEpochSec?: number | null): string {
  const end = endEpochSec ?? Date.now() / 1000;
  const diff = end - startEpochSec;
  if (diff < 60) return `${Math.round(diff)}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ${Math.round(diff % 60)}s`;
  return `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`;
}

export function formatDay(
  epochSeconds: number,
  locale: string,
  today: string,
  yesterday: string,
): string {
  const d = new Date(epochSeconds * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return today;
  const yest = new Date(now);
  yest.setDate(yest.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return yesterday;
  return d.toLocaleDateString(locale, { weekday: "long", month: "short", day: "numeric" });
}

export function formatTime(epochSeconds: number, locale: string): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString(locale, {
    hour: "2-digit",
    minute: "2-digit",
  });
}
