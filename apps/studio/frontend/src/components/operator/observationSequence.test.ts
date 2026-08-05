import { beforeEach, describe, expect, it } from "vitest";

import {
  nextObservationSeq,
  observationObserver,
  resetObservationSequence,
} from "./observationSequence";

describe("observation sequence", () => {
  beforeEach(() => {
    resetObservationSequence();
  });

  it("counts up from one, so a later view always outranks an earlier one", () => {
    expect(nextObservationSeq()).toBe(1);
    expect(nextObservationSeq()).toBe(2);
    expect(nextObservationSeq()).toBe(3);
  });

  it("names one observer for every view this page reports", () => {
    const observer = observationObserver();
    nextObservationSeq();
    nextObservationSeq();
    expect(observationObserver()).toBe(observer);
  });

  it("becomes a different observer when the page is replaced", () => {
    // A reload is a new page looking at a new view. Its count restarts, and
    // the identity change is what stops that restarted count from being
    // measured against the count of the page it replaced — which is how a page
    // the human has already left ends up outranking the one they are on.
    const before = observationObserver();
    nextObservationSeq();
    nextObservationSeq();

    resetObservationSequence();

    expect(observationObserver()).not.toBe(before);
    expect(nextObservationSeq()).toBe(1);
  });
});
