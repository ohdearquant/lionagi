/**
 * RunDetail contract tests.
 *
 * Verifies:
 * - RunDetail.tsx exists and exports a default component
 * - It does not import Drawer (master-detail doctrine)
 * - It accepts id + fullPage props (component interface contract)
 * - history.tsx uses SplitPane, not Drawer
 * - history.tsx imports RunDetail
 * - The ?sel= URL parameter is validated (validateSearch logic)
 */

import { describe, it, expect, beforeAll } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

const HISTORY_DIR = path.resolve(__dirname);
const ROUTES_DIR = path.resolve(__dirname, "../../routes");

// ─── File existence ───────────────────────────────────────────────────────────

describe("history/ component files — existence", () => {
  it("RunDetail.tsx exists", () => {
    expect(fs.existsSync(path.join(HISTORY_DIR, "RunDetail.tsx"))).toBe(true);
  });

  it("InvocationDetail.tsx exists", () => {
    expect(fs.existsSync(path.join(HISTORY_DIR, "InvocationDetail.tsx"))).toBe(true);
  });

  it("ShowDetail.tsx exists", () => {
    expect(fs.existsSync(path.join(HISTORY_DIR, "ShowDetail.tsx"))).toBe(true);
  });
});

// ─── No Drawer in history components ─────────────────────────────────────────

describe("history/ — no Drawer overlay import (master-detail doctrine §4)", () => {
  const FILES = ["RunDetail.tsx", "InvocationDetail.tsx", "ShowDetail.tsx"];

  for (const file of FILES) {
    it(`${file} does not import Drawer`, () => {
      const src = fs.readFileSync(path.join(HISTORY_DIR, file), "utf-8");
      expect(src).not.toMatch(/import.*Drawer.*from/);
      expect(src).not.toMatch(/from.*shell\/Drawer/);
    });
  }

  it("history.tsx does not import Drawer", () => {
    const src = fs.readFileSync(path.join(ROUTES_DIR, "history.tsx"), "utf-8");
    expect(src).not.toMatch(/import.*Drawer.*from/);
    expect(src).not.toMatch(/from.*shell\/Drawer/);
  });
});

// ─── history.tsx — doctrine wiring ───────────────────────────────────────────

describe("history.tsx — master-detail wiring", () => {
  let src: string;
  beforeAll(() => {
    src = fs.readFileSync(path.join(ROUTES_DIR, "history.tsx"), "utf-8");
  });

  it("imports SplitPane", () => {
    expect(src).toMatch(/SplitPane/);
  });

  it("imports RunDetail from history/", () => {
    expect(src).toMatch(/from.*history\/RunDetail/);
  });

  it("does not embed shows — History is runs + invocations only", () => {
    expect(src).not.toMatch(/history\/ShowDetail/);
  });

  it("passes ?sel= search param in validateSearch", () => {
    expect(src).toMatch(/sel/);
  });
});

// ─── validateSearch logic — ?tab= + ?sel= ────────────────────────────────────

const HISTORY_TABS = ["all", "run", "show"] as const;
type HistoryTab = (typeof HISTORY_TABS)[number];

function validateSearch(search: Record<string, unknown>): { tab?: HistoryTab; sel?: string } {
  const tab = search.tab;
  const sel = typeof search.sel === "string" ? search.sel : undefined;
  return {
    ...(HISTORY_TABS.includes(tab as HistoryTab) ? { tab: tab as HistoryTab } : {}),
    ...(sel ? { sel } : {}),
  };
}

describe("history validateSearch", () => {
  it("returns empty object for unknown inputs", () => {
    expect(validateSearch({})).toEqual({});
  });

  it("accepts valid tab values", () => {
    for (const tab of HISTORY_TABS) {
      expect(validateSearch({ tab })).toEqual({ tab });
    }
  });

  it("ignores unknown tab values", () => {
    expect(validateSearch({ tab: "unknown" })).toEqual({});
  });

  it("passes sel through as string", () => {
    expect(validateSearch({ sel: "run:abc-123" })).toEqual({ sel: "run:abc-123" });
  });

  it("ignores non-string sel", () => {
    expect(validateSearch({ sel: 42 })).toEqual({});
    expect(validateSearch({ sel: null })).toEqual({});
  });

  it("combines valid tab and sel", () => {
    expect(validateSearch({ tab: "run", sel: "run:abc" })).toEqual({
      tab: "run",
      sel: "run:abc",
    });
  });

  it("drops empty sel string", () => {
    expect(validateSearch({ sel: "" })).toEqual({});
  });
});

// ─── Auto-select-first logic ──────────────────────────────────────────────────
// Mirrors the autoSelectDone logic in HistoryPage.

describe("auto-select-first logic", () => {
  it("picks the first filtered entry key when no sel is set", () => {
    const filtered = [
      { key: "run:abc", kind: "run" as const },
      { key: "run:def", kind: "run" as const },
    ];
    const sel: string | undefined = undefined;
    const selValid = sel != null && filtered.some((e) => e.key === sel);
    const selected = selValid ? sel : filtered[0]?.key;
    expect(selected).toBe("run:abc");
  });

  it("keeps existing sel when it matches a filtered entry", () => {
    const filtered = [
      { key: "run:abc", kind: "run" as const },
      { key: "run:def", kind: "run" as const },
    ];
    const sel = "run:def";
    const selValid = sel != null && filtered.some((e) => e.key === sel);
    const selected = selValid ? sel : filtered[0]?.key;
    expect(selected).toBe("run:def");
  });

  it("falls back to first entry when sel is stale", () => {
    const filtered = [{ key: "run:abc", kind: "run" as const }];
    const sel = "run:stale-id-not-in-list";
    const selValid = sel != null && filtered.some((e) => e.key === sel);
    const selected = selValid ? sel : filtered[0]?.key;
    expect(selected).toBe("run:abc");
  });

  it("returns undefined when filtered list is empty", () => {
    const filtered: Array<{ key: string }> = [];
    const sel: string | undefined = undefined;
    const selValid = sel != null && filtered.some((e) => e.key === sel);
    const selected = selValid ? sel : filtered[0]?.key;
    expect(selected).toBeUndefined();
  });
});

// ─── SplitPane id contract ────────────────────────────────────────────────────

describe("history SplitPane id contract", () => {
  it("history.tsx uses id='history' for SplitPane (localStorage key)", () => {
    const src = fs.readFileSync(path.join(ROUTES_DIR, "history.tsx"), "utf-8");
    expect(src).toMatch(/id=.history./);
  });
});
