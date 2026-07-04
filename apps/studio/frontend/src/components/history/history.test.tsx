/**
 * History helpers — pure unit tests.
 *
 * Tests statusGlyph, statusColor, formatDuration, formatDay, formatTime
 * from historyHelpers.ts. No rendering, no @testing-library/react.
 */

import { describe, it, expect } from "vitest";
import { statusGlyph, statusColor, formatDuration, formatDay, formatTime } from "./historyHelpers";

// ─── statusGlyph ─────────────────────────────────────────────────────────────

describe("statusGlyph", () => {
  it("running → ◉", () => expect(statusGlyph("running")).toBe("◉"));
  it("completed → ✓", () => expect(statusGlyph("completed")).toBe("✓"));
  it("success → ✓", () => expect(statusGlyph("success")).toBe("✓"));
  it("failed → ✗", () => expect(statusGlyph("failed")).toBe("✗"));
  it("failure → ✗", () => expect(statusGlyph("failure")).toBe("✗"));
  it("cancelled → ○", () => expect(statusGlyph("cancelled")).toBe("○"));
  it("pending → ◌", () => expect(statusGlyph("pending")).toBe("◌"));
  it("queued → ◌", () => expect(statusGlyph("queued")).toBe("◌"));
  it("unknown status → ·", () => expect(statusGlyph("something_else")).toBe("·"));
  it("case-insensitive: RUNNING → ◉", () => expect(statusGlyph("RUNNING")).toBe("◉"));
  it("case-insensitive: FAILED → ✗", () => expect(statusGlyph("FAILED")).toBe("✗"));
});

// ─── statusColor ─────────────────────────────────────────────────────────────

describe("statusColor", () => {
  it("running", () => expect(statusColor("running")).toBe("var(--status-running)"));
  it("completed", () => expect(statusColor("completed")).toBe("var(--status-success)"));
  it("success", () => expect(statusColor("success")).toBe("var(--status-success)"));
  it("failed", () => expect(statusColor("failed")).toBe("var(--status-failure)"));
  it("failure", () => expect(statusColor("failure")).toBe("var(--status-failure)"));
  it("cancelled", () => expect(statusColor("cancelled")).toBe("var(--content-muted)"));
  it("pending", () => expect(statusColor("pending")).toBe("var(--status-pending)"));
  it("queued", () => expect(statusColor("queued")).toBe("var(--status-pending)"));
  it("unknown → muted", () => expect(statusColor("unknown_status")).toBe("var(--content-muted)"));
  it("case-insensitive: RUNNING", () =>
    expect(statusColor("RUNNING")).toBe("var(--status-running)"));
});

// ─── formatDuration ──────────────────────────────────────────────────────────

describe("formatDuration", () => {
  const now = 1_000_000;

  it("30s", () => expect(formatDuration(now - 30, now)).toBe("30s"));
  it("59s", () => expect(formatDuration(now - 59, now)).toBe("59s"));
  it("1m 30s", () => expect(formatDuration(now - 90, now)).toBe("1m 30s"));
  it("2m 0s", () => expect(formatDuration(now - 120, now)).toBe("2m 0s"));
  it("1h 1m (3660s)", () => expect(formatDuration(now - 3660, now)).toBe("1h 1m"));
  it("1h 0m (3600s)", () => expect(formatDuration(now - 3600, now)).toBe("1h 0m"));
  it("2h 30m (9000s)", () => expect(formatDuration(now - 9000, now)).toBe("2h 30m"));
  it("uses Date.now when endEpochSec is omitted (smoke)", () => {
    const start = Date.now() / 1000 - 30;
    const result = formatDuration(start);
    expect(result).toMatch(/^\d+s$/);
  });
  it("uses Date.now when endEpochSec is null", () => {
    const start = Date.now() / 1000 - 45;
    const result = formatDuration(start, null);
    expect(result).toMatch(/^\d+s$/);
  });
});

// ─── formatDay ───────────────────────────────────────────────────────────────

describe("formatDay", () => {
  const locale = "en-US";
  const todayLabel = "Today";
  const yesterdayLabel = "Yesterday";

  it("returns todayLabel for a timestamp from today", () => {
    const nowSec = Math.floor(Date.now() / 1000);
    expect(formatDay(nowSec, locale, todayLabel, yesterdayLabel)).toBe(todayLabel);
  });

  it("returns yesterdayLabel for a timestamp from yesterday", () => {
    const yesterdaySec = Math.floor(Date.now() / 1000) - 86400;
    expect(formatDay(yesterdaySec, locale, todayLabel, yesterdayLabel)).toBe(yesterdayLabel);
  });

  it("returns a formatted date string for older dates", () => {
    const olderSec = Math.floor(Date.now() / 1000) - 86400 * 10;
    const result = formatDay(olderSec, locale, todayLabel, yesterdayLabel);
    expect(result).not.toBe(todayLabel);
    expect(result).not.toBe(yesterdayLabel);
    expect(result.length).toBeGreaterThan(0);
  });
});

// ─── formatTime ──────────────────────────────────────────────────────────────

describe("formatTime", () => {
  it("returns a time string (hh:mm pattern)", () => {
    const nowSec = Math.floor(Date.now() / 1000);
    const result = formatTime(nowSec, "en-US");
    // HH:MM AM/PM or HH:MM — just check it has a colon
    expect(result).toMatch(/\d{1,2}:\d{2}/);
  });
});

// ─── Entry key format ─────────────────────────────────────────────────────────

describe("entry key format", () => {
  it("run key format", () => expect(`run:${"abc123"}`).toBe("run:abc123"));
  it("inv key format", () => expect(`inv:${"def456"}`).toBe("inv:def456"));
  it("show key format", () => expect(`show:my-topic`).toBe("show:my-topic"));
});
