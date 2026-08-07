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
import { createElement } from "react";
import type { ComponentType, ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import Markdown, { isNoArtifactRootDetail } from "./Markdown";
import * as MarkdownModule from "./Markdown";

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

  it("renders a distinct no-artifact-root state, not the generic missing-file message (issue #2848)", () => {
    expect(SRC).toMatch(/status === "no_artifact_root"/);
    expect(SRC).toMatch(/isNoArtifactRootDetail\(result\.detail\)/);
  });
});

// ─── isNoArtifactRootDetail — the get_run_file 404 detail classifier ─────────

describe("isNoArtifactRootDetail", () => {
  it("is true for both no-artifact-root 404 details get_run_file sends", () => {
    expect(isNoArtifactRootDetail("Run 'abc123' has no artifact root")).toBe(true);
    expect(isNoArtifactRootDetail("Run artifact root no longer exists")).toBe(true);
  });

  it("is false for a genuinely missing file and for no detail at all", () => {
    expect(isNoArtifactRootDetail("File 'notes.md' not found")).toBe(false);
    expect(isNoArtifactRootDetail(undefined)).toBe(false);
  });
});

describe("Markdown.tsx — the file viewer renders markdown as markdown", () => {
  it("decides by file extension, accepting .md and .markdown case-insensitively", () => {
    expect(SRC).toMatch(/const isMarkdown = \/\\\.\(md\|markdown\)\$\/i\.test\(path\)/);
  });

  it("routes a markdown file through the Markdown renderer rather than a <pre>", () => {
    expect(SRC).toMatch(/isMarkdown \?/);
    expect(SRC).toMatch(/<Markdown>\{state\.content\}<\/Markdown>/);
  });

  it("keeps the verbatim <pre> path for every non-markdown file", () => {
    // The <pre> must survive as the else-branch: source files, logs and JSON
    // are read as source, and reflowing them would corrupt what they show.
    expect(SRC).toMatch(/<pre className="whitespace-pre-wrap break-words font-mono/);
  });

  it("renders the previewed document WITHOUT a fileContext, so a viewer cannot stack on itself", () => {
    // The nested render wires no FileRef handlers and mounts no second modal.
    // A bare <Markdown> tag (no props) is the file-link guard; an outer
    // render-policy wrapper does not supply a fileContext.
    expect(SRC).toMatch(/<Markdown>\{state\.content\}<\/Markdown>/);
    expect(SRC).not.toMatch(/<Markdown[^>]+fileContext/);
  });

  it("does not load a remote image embedded in a previewed artifact", () => {
    const RemoteImageGuard = (
      MarkdownModule as typeof MarkdownModule & {
        RemoteImageGuard?: ComponentType<{ children: ReactNode }>;
      }
    ).RemoteImageGuard;
    expect(RemoteImageGuard).toBeTypeOf("function");
    if (!RemoteImageGuard) return;

    const Guard = RemoteImageGuard as ComponentType<{ children?: ReactNode }>;
    const MarkdownRenderer = Markdown as ComponentType<{ children?: string }>;
    const remoteUrls = [
      "https://example.invalid/tracker.png",
      "http://example.invalid/tracker.png",
      "//example.invalid/tracker.png",
      "https:/example.invalid/tracker.png",
      "http:/example.invalid/tracker.png",
      "https:example.invalid/tracker.png",
      "http:example.invalid/tracker.png",
    ];
    for (const remoteUrl of remoteUrls) {
      const html = renderToStaticMarkup(
        createElement(
          Guard,
          null,
          createElement(MarkdownRenderer, null, `![remote tracker](${remoteUrl})`),
        ),
      );

      expect(html).not.toContain("<img");
      expect(html).not.toContain(remoteUrl);
      expect(html).toContain("Remote image blocked");
    }

    const remoteUrl = remoteUrls[0];
    const ordinaryHtml = renderToStaticMarkup(
      createElement(MarkdownRenderer, null, `![remote tracker](${remoteUrl})`),
    );
    expect(ordinaryHtml).toContain("<img");
    expect(ordinaryHtml).toContain(remoteUrl);
  });

  it("gives a rendered document more width than raw source, since tables need it", () => {
    expect(SRC).toMatch(/maxWidth=\{isMarkdown \? "max-w-4xl" : "max-w-2xl"\}/);
  });
});
