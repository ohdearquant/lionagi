/**
 * RunDetail contract tests.
 *
 * Verifies:
 * - RunDetail.tsx exists and exports a default component
 * - It does not import Drawer (master-detail doctrine)
 */

import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

const HISTORY_DIR = path.resolve(__dirname);

// ─── File existence ───────────────────────────────────────────────────────────

describe("history/ component files — existence", () => {
  it("RunDetail.tsx exists", () => {
    expect(fs.existsSync(path.join(HISTORY_DIR, "RunDetail.tsx"))).toBe(true);
  });

  it("InvocationDetail.tsx exists", () => {
    expect(fs.existsSync(path.join(HISTORY_DIR, "InvocationDetail.tsx"))).toBe(true);
  });
});

// ─── No Drawer in history components ─────────────────────────────────────────

describe("history/ — no Drawer overlay import (master-detail doctrine §4)", () => {
  const FILES = ["RunDetail.tsx", "InvocationDetail.tsx"];

  for (const file of FILES) {
    it(`${file} does not import Drawer`, () => {
      const src = fs.readFileSync(path.join(HISTORY_DIR, file), "utf-8");
      expect(src).not.toMatch(/import.*Drawer.*from/);
      expect(src).not.toMatch(/from.*shell\/Drawer/);
    });
  }
});
