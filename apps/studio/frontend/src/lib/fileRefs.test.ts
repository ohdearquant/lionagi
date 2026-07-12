import { describe, expect, it } from "vitest";
import { looksLikeFilename, resolveFileRef, stripTrailingPunctuation } from "./fileRefs";

describe("looksLikeFilename", () => {
  it("accepts common source/doc extensions", () => {
    expect(looksLikeFilename("review_findings.md")).toBe(true);
    expect(looksLikeFilename("main.py")).toBe(true);
    expect(looksLikeFilename("config.json")).toBe(true);
    expect(looksLikeFilename("path/to/file.ts")).toBe(true);
  });

  it("rejects prose and non-filename tokens", () => {
    expect(looksLikeFilename("e.g.")).toBe(false);
    expect(looksLikeFilename("v1.2.3")).toBe(false);
    expect(looksLikeFilename("hello world.md")).toBe(false); // whitespace
    expect(looksLikeFilename("")).toBe(false);
    expect(looksLikeFilename("just some code")).toBe(false);
  });

  it("rejects overly long tokens", () => {
    expect(looksLikeFilename(`${"a".repeat(300)}.md`)).toBe(false);
  });
});

describe("stripTrailingPunctuation", () => {
  it("strips trailing sentence punctuation", () => {
    expect(stripTrailingPunctuation("review.md.")).toBe("review.md");
    expect(stripTrailingPunctuation("notes.txt,")).toBe("notes.txt");
    expect(stripTrailingPunctuation("(spec.md)")).toBe("(spec.md");
  });
});

describe("resolveFileRef", () => {
  const knownFiles = [
    "/runs/r1/agentA/review_findings.md",
    "/runs/r1/agentA/review.md",
    "/runs/r1/agentB/review.md",
    "/runs/r1/synthesis.md",
  ];

  it("resolves an absolute markdown-link path shape via exact match", () => {
    const result = resolveFileRef("/runs/r1/synthesis.md", { knownFiles });
    expect(result).toEqual({ type: "single", path: "/runs/r1/synthesis.md" });
  });

  it("returns none for an absolute path not on the known surface (never fabricates)", () => {
    const result = resolveFileRef("/etc/passwd", { knownFiles });
    expect(result).toEqual({ type: "none" });
  });

  it("resolves a bare filename shape by basename when unique", () => {
    const result = resolveFileRef("review_findings.md", { knownFiles });
    expect(result).toEqual({ type: "single", path: "/runs/r1/agentA/review_findings.md" });
  });

  it("prefers the emitting agent's own dir first on ambiguous basename", () => {
    const result = resolveFileRef("review.md", {
      knownFiles,
      agentDir: "/runs/r1/agentB",
    });
    expect(result).toEqual({ type: "single", path: "/runs/r1/agentB/review.md" });
  });

  it("falls back to ambiguous disambiguation when agent dir doesn't resolve it", () => {
    const result = resolveFileRef("review.md", { knownFiles });
    expect(result.type).toBe("ambiguous");
    if (result.type === "ambiguous") {
      expect(result.candidates.sort()).toEqual(
        ["/runs/r1/agentA/review.md", "/runs/r1/agentB/review.md"].sort(),
      );
    }
  });

  it("stays 'none' (plain text) when there is no match at all", () => {
    const result = resolveFileRef("nonexistent.md", { knownFiles });
    expect(result).toEqual({ type: "none" });
  });

  it("strips trailing sentence punctuation before matching", () => {
    const result = resolveFileRef("synthesis.md.", { knownFiles });
    expect(result).toEqual({ type: "single", path: "/runs/r1/synthesis.md" });
  });

  it("handles file:// URL shape", () => {
    const result = resolveFileRef("file:///runs/r1/synthesis.md", { knownFiles });
    expect(result).toEqual({ type: "single", path: "/runs/r1/synthesis.md" });
  });
});
