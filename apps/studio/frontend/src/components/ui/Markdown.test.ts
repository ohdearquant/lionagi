/**
 * Markdown.tsx file-link wiring — source-contract tests (see
 * history/InvocationDetail.test.tsx / shell/NoDaemonGate.test.tsx: this
 * project has no @testing-library/react, so component wiring is verified
 * against the source rather than a live render). The resolution algorithm
 * itself (agent-dir-first precedence, disambiguation, no-match) is unit
 * tested directly in fileRefs.test.ts.
 */
import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

const SRC = fs.readFileSync(path.resolve(__dirname, "Markdown.tsx"), "utf-8");

describe("Markdown.tsx — file-link resolution wiring", () => {
  it("is opt-in via a fileContext prop (existing callers unaffected)", () => {
    expect(SRC).toMatch(/fileContext\?:\s*FileResolutionContext/);
  });

  it("resolves markdown-link targets (the `a` renderer) through resolveFileRef", () => {
    expect(SRC).toMatch(/a:\s*\(props\)\s*=>/);
    expect(SRC).toMatch(/resolveFileRef/);
  });

  it("resolves bare inline-code filenames (the `code` renderer) via the conservative heuristic", () => {
    expect(SRC).toMatch(/code:\s*\(props\)\s*=>/);
    expect(SRC).toMatch(/looksLikeFilename\(text\)/);
  });

  it("only treats code spans with no language className as filename candidates (not every code span)", () => {
    expect(SRC).toMatch(/!codeClassName && looksLikeFilename\(text\)/);
  });

  it("leaves http(s)/mailto links as normal anchors, never intercepted", () => {
    expect(SRC).toMatch(/\/\^\(https\?:\|mailto:\)\//i);
  });

  it("falls back to the original element when there is no match (stays plain text)", () => {
    expect(SRC).toMatch(/return <>\{fallback\}<\/>/);
  });

  it("renders a disambiguation menu for ambiguous multi-file matches", () => {
    expect(SRC).toMatch(/candidates/);
    expect(SRC).toMatch(/menuOpen/);
  });

  it("fetches content on click via getRunFile", () => {
    expect(SRC).toMatch(/getRunFile\(runId, path\)/);
  });

  it("renders a graceful missing-file state on a click-time 404", () => {
    expect(SRC).toMatch(/result\.status === 404/);
    expect(SRC).toMatch(/status: "missing"/);
    expect(SRC).toMatch(/File not found/);
  });

  it("renders a distinct error state for non-404 failures (not just a crash)", () => {
    expect(SRC).toMatch(/status: "error"/);
  });

  it("handles a rejected getRunFile promise (network failure) instead of leaving the modal stuck loading", () => {
    // getRunFile rethrows on a fetch() network error rather than resolving
    // an { ok: false } shape (see lib/api.ts) — the effect chain must attach
    // a .catch, not just a bare .then, or a dropped connection leaves the
    // modal in "loading" forever.
    expect(SRC).toMatch(/getRunFile\(runId, path\)\s*\.then\(/);
    expect(SRC).toMatch(/\.catch\(\s*\(err\)\s*=>\s*\{/);
    expect(SRC).toMatch(/setState\(\{ status: "error", detail: err instanceof Error/);
  });

  it("never fabricates a target from text alone — file surface comes only from fileContext.knownFiles", () => {
    expect(SRC).toMatch(/knownFiles: fileContext\.knownFiles/);
  });
});
