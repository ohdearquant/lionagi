import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

describe("Studio document security", () => {
  it("does not execute third-party scripts in the authenticated application document", () => {
    const html = fs.readFileSync(path.resolve(__dirname, "../../index.html"), "utf8");
    const scriptSources = Array.from(html.matchAll(/<script\b[^>]*\bsrc=["']([^"']+)/gi), (m) =>
      m[1].trim(),
    );

    expect(scriptSources).toEqual(["/src/main.tsx"]);
  });
});
