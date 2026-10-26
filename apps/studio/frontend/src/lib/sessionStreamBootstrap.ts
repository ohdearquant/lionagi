import { getSession, getSessionSignalPage, readSessionStreamSnapshot } from "./api";
import type { SessionDetail, SessionStreamSnapshot } from "./api";
import { SignalProjection } from "./signalProjection";

export interface SessionLiveBootstrap {
  detail: SessionDetail;
  snapshot: SessionStreamSnapshot | undefined;
  projection: SignalProjection;
}

/** Explicit history pages seed the existing reducer before either live subscription. */
export async function loadSessionLiveBootstrap(
  id: string,
  isCurrent: () => boolean,
): Promise<SessionLiveBootstrap | null> {
  const detail = await getSession(id, { liveSnapshot: true });
  if (!isCurrent()) return null;
  if (detail.id !== id) throw new Error("Session response does not match the selected session");
  const snapshot = readSessionStreamSnapshot(detail);
  const projection = new SignalProjection();
  if (snapshot) {
    let afterSeq = 0;
    while (afterSeq < snapshot.signal_seq) {
      const page = await getSessionSignalPage(id, {
        afterSeq, throughSeq: snapshot.signal_seq, limit: 500,
      });
      if (!isCurrent()) return null;
      for (const signal of page.signals) projection.append(signal);
      if (!page.has_more) break;
      if (page.next_after_seq <= afterSeq) throw new Error("Signal history page did not advance");
      afterSeq = page.next_after_seq;
    }
  }
  return { detail, snapshot, projection };
}
