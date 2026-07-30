/**
 * How many views this browser has observed, counted per conversation.
 *
 * The Operator has to decide whether a reported view was seen after the
 * instruction it is answering. Arrival order cannot say: each report is its own
 * request, so two navigations can reach the server reversed and a view seen
 * before an instruction can arrive after it. A wall clock cannot say either: it
 * can step backwards, and then a page the human has already left holds the
 * higher number.
 *
 * So observations are counted rather than timed, and the count is resumed from
 * the conversation record, which is the one thing that outlives the page.
 *
 * The count belongs to the conversation, not to the browser. A single shared
 * counter would carry one conversation's total into the next one: a page that
 * had counted far in a busy conversation would number its first view of a quiet
 * one far above anything that conversation had ever seen, and every other page
 * on it — having resumed from the real, lower count — would be discarded as
 * behind while the inflated view kept the confident label.
 */
const counts = new Map<string, number>();

/**
 * Resume this conversation's count from a value the server holds.
 *
 * Only ever raises. A lower value is either an older read of the same
 * conversation or a report that lost a race, and adopting it would let this
 * page renumber below views it has already reported.
 */
export function seedObservationCount(conversationId: string, seq: number | null | undefined): void {
  if (typeof seq !== "number" || !Number.isFinite(seq)) return;
  const floor = Math.floor(seq);
  if (floor > (counts.get(conversationId) ?? 0)) counts.set(conversationId, floor);
}

/** Number the next view observed in this conversation. */
export function nextObservationSeq(conversationId: string): number {
  const next = (counts.get(conversationId) ?? 0) + 1;
  counts.set(conversationId, next);
  return next;
}

/** Test seam: forget every count, as a fresh page load would. */
export function resetObservationCounts(): void {
  counts.clear();
}
