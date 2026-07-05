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

  it("links siblings to /fleet with s=id", () => {
    expect(src).toMatch(/to="\/fleet"/);
    expect(src).toMatch(/search=\{\{ s: s\.id \}\}/);
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
