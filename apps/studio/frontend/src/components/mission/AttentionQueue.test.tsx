/**
 * AttentionQueue.tsx — discharge lifecycle contract tests.
 *
 * Pure source-contract tests: no rendering, no @testing-library/react
 * (mirrors InvocationDetail.test.tsx). Verifies the component wires the
 * discharge actions to the persistence API, keeps acknowledged rows
 * visible, and exposes a discharged-items filter — the parts a reducer
 * unit test can't see.
 */

import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

const MISSION_DIR = path.resolve(__dirname);

const src = fs.readFileSync(path.join(MISSION_DIR, "AttentionQueue.tsx"), "utf-8");

describe("AttentionQueue.tsx — source contract", () => {
  it("exports a default function component", () => {
    expect(src).toMatch(/export default function AttentionQueue/);
  });

  it("accepts a dischargedItems prop distinct from items", () => {
    expect(src).toMatch(/dischargedItems:\s*AttentionItem\[\]/);
  });

  it("calls putAttentionDisposition for acknowledge/resolve/snooze/expected", () => {
    expect(src).toMatch(/putAttentionDisposition/);
    expect(src).toMatch(/state:\s*"acknowledged"|"acknowledged"\)/);
    expect(src).toMatch(/save\("resolved"\)/);
    expect(src).toMatch(/save\("snoozed"/);
    expect(src).toMatch(/save\("expected"/);
  });

  it("calls deleteAttentionDisposition for undo", () => {
    expect(src).toMatch(/deleteAttentionDisposition/);
  });

  it("never removes a row before the write succeeds — no optimistic local disposition state", () => {
    // The component must read discharge state only from item.disposition
    // (server-confirmed, joined by the reducer), never mutate a local copy
    // of it after a PUT/DELETE call.
    expect(src).not.toMatch(/setLocalDisposition|useState<AttentionDisposition/);
    expect(src).toMatch(/item\.disposition/);
  });

  it("'expected' requires a non-blank note before submitting", () => {
    expect(src).toMatch(/noteRequired/);
    expect(src).toMatch(/note\.trim\(\)/);
  });

  it("snooze and expected always submit an expiresAt", () => {
    expect(src).toMatch(/expiresAt:\s*Math\.floor\(Date\.now\(\)\s*\/\s*1000\)\s*\+\s*durationSec/);
    expect(src).toMatch(
      /expiresAt:\s*Math\.floor\(Date\.now\(\)\s*\/\s*1000\)\s*\+\s*SNOOZE_DURATIONS\[0\]\.seconds/,
    );
  });

  it("acknowledged items stay visible (restyled), never filtered out of the row list", () => {
    // AttentionQueue only receives already-split active items (acknowledged
    // included) from boardReducer; it must render every item it's given
    // without an additional acknowledged-hiding filter.
    expect(src).not.toMatch(
      /\.filter\(\s*\(?i\)?\s*=>\s*!?.*disposition.*state\s*===\s*"acknowledged"/,
    );
    expect(src).toMatch(/acknowledged/);
  });

  it("shows a discharged-items toggle, off by default", () => {
    expect(src).toMatch(/showDischarged/);
    expect(src).toMatch(/useState\(false\)/);
  });

  it("every disposition action button carries an aria-label naming the item", () => {
    expect(src).toMatch(/acknowledgeAria.*\{\s*name:\s*item\.name\s*\}/s);
    expect(src).toMatch(/resolveAria.*\{\s*name:\s*item\.name\s*\}/s);
    expect(src).toMatch(/undoAria.*\{\s*name:\s*item\.name\s*\}/s);
  });

  it("reports a failed write with an inline error, not a thrown/swallowed exception", () => {
    expect(src).toMatch(/catch\s*\(err\)\s*\{[\s\S]*setError/);
  });

  it("still renders the existing Open deep link unchanged", () => {
    expect(src).toMatch(/attention\.open/);
    expect(src).toMatch(/ItemLink/);
  });
});
