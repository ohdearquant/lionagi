import type { StudioEvent } from "./types.js";

/**
 * Connects to GET /api/sessions/{sessionId}/stream (SSE) and dispatches
 * events to onEvent until {type:"done"} is received or the abort signal fires.
 *
 * Implements SSE parsing over the Fetch ReadableStream — no external deps.
 */
export async function streamSession(
  baseUrl: string,
  sessionId: string,
  token: string | undefined,
  onEvent: (e: StudioEvent) => void,
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
    `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/stream`,
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
        break;
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

        const raw = dataLines.join("");
        let event: StudioEvent;
        try {
          event = JSON.parse(raw) as StudioEvent;
        } catch {
          continue;
        }

        onEvent(event);

        if (event.type === "done") {
          return;
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
