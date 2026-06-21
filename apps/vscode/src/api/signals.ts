import type { SignalStreamEvent } from "./types.js";

/**
 * Connects to GET /api/sessions/{sessionId}/signals (SSE) and dispatches
 * events to onEvent until {type:"done"} is received or the abort signal fires.
 *
 * Replays all persisted signal rows from seq 0, then polls for new ones.
 * Implements SSE parsing over the Fetch ReadableStream — no external deps.
 */
export async function streamSignals(
  baseUrl: string,
  sessionId: string,
  token: string | undefined,
  onEvent: (e: SignalStreamEvent) => void,
  signal: AbortSignal
): Promise<void> {
  const headers: Record<string, string> = {
    Accept: "text/event-stream",
    "Cache-Control": "no-cache",
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(
    `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/signals`,
    { method: "GET", headers, signal }
  );

  if (!res.ok) {
    throw new Error(`SSE connect failed: ${res.status} ${res.statusText}`);
  }

  if (!res.body) {
    throw new Error("SSE response has no body");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        // Transport EOF reached without ever seeing a `done` event below —
        // a dropped connection, backend restart, or proxy close. Surface it so
        // the caller can show an error / reconnect rather than silently freezing
        // on the last snapshot. (A clean finish returns from inside the loop.)
        throw new Error("Signal stream closed before completion");
      }
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by double newlines.
      const frames = buffer.split("\n\n");
      // Keep the last (possibly incomplete) chunk in the buffer.
      buffer = frames.pop() ?? "";

      for (const frame of frames) {
        if (!frame.trim()) {
          continue;
        }
        // Extract the `data:` line(s) from the frame.
        const dataLines = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim());

        if (dataLines.length === 0) {
          continue;
        }

        const raw = dataLines.join("\n");
        let event: SignalStreamEvent;
        try {
          event = JSON.parse(raw) as SignalStreamEvent;
        } catch {
          continue;
        }

        onEvent(event);

        if ("type" in event && event.type === "done") {
          return;
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
