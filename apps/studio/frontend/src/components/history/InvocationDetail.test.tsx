/**
 * InvocationSection contract tests.
 *
 * Pure source-contract tests: no rendering, no @testing-library/react.
 * Verifies component shape, API usage, and link contract.
 */

import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

const HISTORY_DIR = path.resolve(__dirname);

const src = fs.readFileSync(path.join(HISTORY_DIR, "InvocationDetail.tsx"), "utf-8");

describe("InvocationDetail.tsx (InvocationSection) — source contract", () => {
  it("exports a default function component", () => {
    expect(src).toMatch(/export default function InvocationSection/);
  });

  it("accepts invocationId prop", () => {
    expect(src).toMatch(/invocationId/);
  });

  it("accepts currentSessionId prop", () => {
    expect(src).toMatch(/currentSessionId/);
  });

  it("does not import Drawer (master-detail doctrine)", () => {
    expect(src).not.toMatch(/import.*Drawer/);
    expect(src).not.toMatch(/from.*shell\/Drawer/);
  });

  it("calls getInvocation (fetches detail from API)", () => {
    expect(src).toMatch(/getInvocation/);
  });

  it("renders /{skill} (shows skill name)", () => {
    expect(src).toMatch(/\/\{data\.skill\}/);
  });

  it("does not link back to the removed standalone invocation page", () => {
    expect(src).not.toMatch(/to=.\/invocations\/\$id./);
  });

  it("links siblings to /history with tab=run sel=run:${id}", () => {
    expect(src).toMatch(/tab.*run/);
    expect(src).toMatch(/run:\${s\.id}/);
  });

  it("renders StatusPill for status display", () => {
    expect(src).toMatch(/StatusPill/);
  });

  it("resets state on id change (cancels stale fetch)", () => {
    expect(src).toMatch(/active = false/);
  });

  it("renders nothing while loading (returns null)", () => {
    expect(src).toMatch(/return null/);
  });

  it("renders artifacts section via OutcomeRenderer", () => {
    expect(src).toMatch(/OutcomeRenderer/);
  });

  it("filters out current session from siblings list", () => {
    expect(src).toMatch(/currentSessionId/);
    expect(src).toMatch(/filter/);
  });
});

// ─── extractSummary helper — pure logic ──────────────────────────────────────
// Mirrors the logic in ShowDetail.tsx for plan summary parsing.
// Tested here since ShowDetail.tsx has no separate test file.

function extractSummary(showMd: string | null): Record<string, string> {
  if (!showMd) return {};
  const out: Record<string, string> = {};
  const lines = showMd.split("\n").slice(0, 60);
  const labelRe =
    /^\s*(?:[-*]\s*)?\**\s*(Goal|Status|Blockers?|Next action|Next steps?|Owner|Started|Updated|Progress)\**\s*:\s*(.+?)\s*$/i;
  for (const line of lines) {
    const m = line.match(labelRe);
    if (m) {
      const key = m[1].toLowerCase();
      if (!out[key]) out[key] = m[2].trim();
    }
  }
  return out;
}

describe("extractSummary (ShowDetail plan parser)", () => {
  it("returns empty object for null input", () => {
    expect(extractSummary(null)).toEqual({});
  });

  it("parses Goal field", () => {
    const md = "Goal: Build the master-detail layout";
    expect(extractSummary(md).goal).toBe("Build the master-detail layout");
  });

  it("parses Status field case-insensitively", () => {
    const md = "STATUS: in progress";
    expect(extractSummary(md).status).toBe("in progress");
  });

  it("parses Next action field", () => {
    const md = "Next action: Ship the PR";
    const result = extractSummary(md);
    expect(result["next action"]).toBe("Ship the PR");
  });

  it("parses bold markdown labels (Goal: becomes goal)", () => {
    const md = "**Goal**: Deploy to production";
    expect(extractSummary(md).goal).toBe("Deploy to production");
  });

  it("parses list-prefixed labels (- Goal: ...)", () => {
    const md = "- Goal: Improve latency";
    expect(extractSummary(md).goal).toBe("Improve latency");
  });

  it("keeps only first occurrence (no overwrite)", () => {
    const md = "Goal: First\nGoal: Second";
    expect(extractSummary(md).goal).toBe("First");
  });

  it("handles empty string input", () => {
    expect(extractSummary("")).toEqual({});
  });

  it("ignores lines beyond first 60", () => {
    const preamble = Array.from({ length: 61 }, () => "not a label").join("\n");
    const md = preamble + "\nGoal: Hidden";
    expect(extractSummary(md).goal).toBeUndefined();
  });
});

// ─── ShowDetail source contract ───────────────────────────────────────────────

describe("ShowDetail.tsx — source contract", () => {
  const showSrc = fs.readFileSync(path.join(HISTORY_DIR, "ShowDetail.tsx"), "utf-8");

  it("exports a default function component", () => {
    expect(showSrc).toMatch(/export default function ShowDetail/);
  });

  it("accepts entry prop", () => {
    expect(showSrc).toMatch(/entry: HistoryEntry/);
  });

  it("does not import Drawer", () => {
    expect(showSrc).not.toMatch(/import.*Drawer/);
  });

  it("calls getShow from lib/api", () => {
    expect(showSrc).toMatch(/getShow/);
  });

  it("has open full view link to /shows/$topic", () => {
    expect(showSrc).toMatch(/to=.\/shows\/\$topic./);
  });

  it("shows plan summary section", () => {
    expect(showSrc).toMatch(/Plan/);
  });

  it("renders plays table", () => {
    expect(showSrc).toMatch(/Plays/);
  });
});
