import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

function sourceFiles(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(file);
    if (!entry.name.endsWith(".tsx") || entry.name.includes(".test.")) return [];
    return [file];
  });
}

describe("native button safety", () => {
  it("gives every button an explicit type so controls cannot submit a surrounding form", () => {
    const srcRoot = path.resolve(__dirname, "../..");
    const missing: string[] = [];

    for (const file of sourceFiles(srcRoot)) {
      const source = fs
        .readFileSync(file, "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/\/\/.*$/gm, "");
      for (const match of source.matchAll(/<button\b[^>]*>/gs)) {
        if (/\btype\s*=/.test(match[0])) continue;
        const line = source.slice(0, match.index).split("\n").length;
        missing.push(`${path.relative(srcRoot, file)}:${line}`);
      }
    }

    expect(missing).toEqual([]);
  });
});
