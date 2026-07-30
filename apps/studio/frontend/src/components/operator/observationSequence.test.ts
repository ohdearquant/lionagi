import { beforeEach, describe, expect, it } from "vitest";

import {
  nextObservationSeq,
  resetObservationCounts,
  seedObservationCount,
} from "./observationSequence";

describe("observation sequence", () => {
  beforeEach(() => {
    resetObservationCounts();
  });

  it("counts each conversation on its own, so a busy one cannot inflate a quiet one", () => {
    // A shared counter would carry this 100 into the next conversation, and
    // every other page on that conversation — resumed from its real, lower
    // count — would then be discarded as behind while the inflated view kept
    // the "live" label.
    seedObservationCount("busy", 100);
    expect(nextObservationSeq("busy")).toBe(101);

    seedObservationCount("quiet", 5);
    expect(nextObservationSeq("quiet")).toBe(6);
  });

  it("resumes from the server rather than restarting, as a reload must", () => {
    seedObservationCount("c", 40);
    expect(nextObservationSeq("c")).toBe(41);
  });

  it("ignores a seed that would renumber below views already reported", () => {
    seedObservationCount("c", 40);
    // A stale read of the same conversation, or a report that lost a race.
    seedObservationCount("c", 7);
    expect(nextObservationSeq("c")).toBe(41);
  });

  it("ignores a seed that is not a usable number", () => {
    seedObservationCount("c", null);
    seedObservationCount("c", undefined);
    seedObservationCount("c", Number.NaN);
    expect(nextObservationSeq("c")).toBe(1);
  });

  it("catches up when another page has counted further", () => {
    expect(nextObservationSeq("c")).toBe(1);
    // What the server returns when it discards a report for being behind.
    seedObservationCount("c", 101);
    expect(nextObservationSeq("c")).toBe(102);
  });
});
