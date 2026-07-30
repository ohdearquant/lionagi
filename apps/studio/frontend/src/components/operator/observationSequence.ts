/**
 * Which page is doing the observing, and how many views it has seen.
 *
 * The Operator has to decide whether a reported view was seen after the
 * instruction it is answering. Arrival order cannot say: each report is its own
 * request, so two navigations can reach the server reversed and a view seen
 * before an instruction can arrive after it. A wall clock cannot say either: it
 * can step backwards, and then a page the human has already left holds the
 * higher number.
 *
 * So observations are counted rather than timed. A count is only meaningful
 * inside the page that did the counting, which is why every observation also
 * says who observed it. Two tabs open on one conversation are looking at two
 * different pages, and neither one's count says anything about the other's:
 * whichever tab the instruction was sent from is the only one whose later
 * observations describe where the human is. Comparing counts across observers
 * is what makes a page they have already left look current.
 *
 * A reload is a new observer, deliberately. Its count restarts at one and is
 * never measured against the count of the page it replaced.
 */
let observerId = crypto.randomUUID();
let observed = 0;

/** Identifies the page that observed a view. Stable for as long as it lives. */
export function observationObserver(): string {
  return observerId;
}

/** Number the next view this page observes. */
export function nextObservationSeq(): number {
  observed += 1;
  return observed;
}

/** Test seam: become a different page, as a reload would. */
export function resetObservationSequence(): void {
  observerId = crypto.randomUUID();
  observed = 0;
}
