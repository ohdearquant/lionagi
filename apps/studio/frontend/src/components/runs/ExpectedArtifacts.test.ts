/**
 * ExpectedArtifacts — source-contract tests (this project has no
 * @testing-library/react, so component wiring is verified against the source
 * rather than a live render; see ui/Markdown.test.ts for the same pattern).
 *
 * The behaviour under test: a provisional verification is a reading taken while
 * the run is still going. An artifact that is not on disk yet has not been
 * missed, it has not been written yet, and the panel must not say otherwise.
 */
import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

const SRC = fs.readFileSync(path.resolve(__dirname, "ExpectedArtifacts.tsx"), "utf-8");

describe("ExpectedArtifacts.tsx — provisional readings", () => {
  it("never calls an artifact missing on a provisional reading", () => {
    expect(SRC).toMatch(/!verification\?\.provisional &&/);
  });

  it("keeps MISSING reachable for a recorded verdict", () => {
    expect(SRC).toMatch(/missingRequired\.has\(entry\.id\) \|\| missingOptional\.has\(entry\.id\)/);
    expect(SRC).toMatch(/"MISSING"/);
  });

  it("shows written-so-far progress instead of a contract status while the run is live", () => {
    expect(SRC).toMatch(/verification\?\.provisional \?/);
    expect(SRC).toMatch(/\{producedById\.size\} of \{expected\.length\} written/);
  });

  it("still shows the recorded verdict when there is one", () => {
    expect(SRC).toMatch(/Verified: \{verification\.status\}/);
  });

  it("counts progress from what was produced, not from the contract status", () => {
    // status is "failed" for every incomplete run, so keying the badge off it
    // would put a red verdict on a run that is simply not finished.
    expect(SRC).toMatch(/producedById\.size === expected\.length \? "ok" : "pending"/);
  });
});
