// ─── Stream chunk shape (matches DB schema from backend) ─────────────────────

export interface StreamChunk {
  id: number;
  flow_id: string;
  branch_id: string;
  api_call_id: string;
  ts: number;
  chunk_type: string;
  content: string;
  tool_name: string | null;
  tool_id: string | null;
  tool_input: string | null;
  tool_output: string | null;
  is_delta: number;
  is_error: number;
  metadata: string;
}

// ─── Attending: file/artifact references the agent attended to ───────────────

export interface AttendingItem {
  path: string;
  lineStart: number | null;
  weight: number;
}

interface AttendingOptions {
  limit?: number;
}

/** Parse a raw path string (possibly with :lineNumber suffix) into path + lineStart. */
function parsePathRef(raw: string): { path: string; lineStart: number | null } | null {
  if (raw.startsWith("http") || raw.startsWith("#")) return null;
  const colonIdx = raw.lastIndexOf(":");
  if (colonIdx > 0) {
    const maybeNum = raw.slice(colonIdx + 1);
    const n = parseInt(maybeNum, 10);
    if (!isNaN(n) && String(n) === maybeNum) {
      return { path: raw.slice(0, colonIdx), lineStart: n };
    }
  }
  return { path: raw, lineStart: null };
}

/**
 * Parse markdown link targets of the form [label](path) or [label](path:line)
 * AND inline code references of the form `filename.ext:line` from all text
 * chunks, dedupe by path, and rank by recency (most-recent chunk gets weight
 * 1.0, earlier chunks get linearly decreasing weights).
 */
export function extractAttending(
  chunks: StreamChunk[],
  options: AttendingOptions = {},
): AttendingItem[] {
  const { limit = 5 } = options;

  // Matches markdown links: [anything](/some/path:line)
  const LINK_RE = /\[([^\]]*)\]\(([^)]+)\)/g;
  // Matches inline code file refs: `some/file.ext:123` or `file.ext:123`
  const CODE_REF_RE = /`([^`\s]+\.[a-zA-Z][a-zA-Z0-9]*(?::[0-9]+)?)`/g;

  // Collect all references in order of appearance (chunk order = recency order)
  // Later chunks = more recent = higher weight. We process oldest first, then
  // overwrite on dedup so the most-recent occurrence wins.
  const byPath = new Map<string, { lineStart: number | null; chunkIndex: number }>();

  function recordRef(raw: string, chunkIndex: number): void {
    const parsed = parsePathRef(raw);
    if (!parsed) return;
    const existing = byPath.get(parsed.path);
    // Overwrite only if newer chunk, OR same chunk with a lineStart (prefer specific over vague)
    if (
      !existing ||
      chunkIndex > existing.chunkIndex ||
      (chunkIndex === existing.chunkIndex && parsed.lineStart !== null && existing.lineStart === null)
    ) {
      byPath.set(parsed.path, { lineStart: parsed.lineStart, chunkIndex });
    }
  }

  chunks.forEach((chunk, chunkIndex) => {
    if (chunk.chunk_type !== "text" || !chunk.content) return;

    // Parse markdown links
    LINK_RE.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = LINK_RE.exec(chunk.content)) !== null) {
      recordRef(match[2], chunkIndex);
    }

    // Parse inline code file references (e.g. `persist.py:318`)
    CODE_REF_RE.lastIndex = 0;
    while ((match = CODE_REF_RE.exec(chunk.content)) !== null) {
      recordRef(match[1], chunkIndex);
    }
  });

  if (byPath.size === 0) return [];

  // Assign weights: highest chunkIndex -> weight 1.0, linearly down to close to 0
  const entries = Array.from(byPath.entries()).map(([path, { lineStart, chunkIndex }]) => ({
    path,
    lineStart,
    chunkIndex,
  }));

  // Sort descending by chunkIndex (most recent first)
  entries.sort((a, b) => b.chunkIndex - a.chunkIndex || b.lineStart! - a.lineStart!);

  const maxIdx = entries[0].chunkIndex;
  const minIdx = entries[entries.length - 1].chunkIndex;
  const range = maxIdx - minIdx || 1;

  const weighted = entries.map((e) => ({
    path: e.path,
    lineStart: e.lineStart,
    weight: parseFloat(((e.chunkIndex - minIdx) / range).toFixed(6)) || 1,
  }));

  // Ensure the top item has weight exactly 1
  if (weighted.length > 0) weighted[0].weight = 1;

  return weighted.slice(0, limit);
}

// ─── Thinking: decision summary lines the agent committed to ─────────────────

export interface ThinkingItem {
  text: string;
  ts: number;
  status: "complete";
}

/**
 * Extract the first line of each non-delta text chunk as a decision summary,
 * filtering to chunks whose opening line begins with an imperative past-tense
 * marker ("Implemented", "Phase N", etc.), then return in chronological order.
 */
export function extractThinking(chunks: StreamChunk[]): ThinkingItem[] {
  const DECISION_RE = /^(Implemented|Phase\s+\d+|Created\s+Phase\s+\d+.*?implemented|Ran |Added |Updated |Fixed |Removed )/i;

  const items: ThinkingItem[] = [];

  for (const chunk of chunks) {
    if (chunk.chunk_type !== "text" || chunk.is_delta !== 0) continue;
    if (!chunk.content) continue;

    const firstLine = chunk.content.split("\n")[0].trim();
    if (!firstLine) continue;

    // Include chunks whose first line describes an implementation or phase action
    if (
      firstLine.startsWith("Implemented") ||
      /^Phase\s+\d+/.test(firstLine)
    ) {
      items.push({ text: firstLine, ts: chunk.ts, status: "complete" });
    }
  }

  return items;
}

// ─── Emitting: artifacts and files the agent produced ────────────────────────

export interface EmittingItem {
  name: string;
  path: string;
  kind: "artifact" | "file";
  status: "done";
}

interface EmittingOptions {
  limit?: number;
}

/**
 * Detect emitted artifacts (.khive/…/artifacts/ paths) and edited source files
 * referenced via markdown links in text chunks. Dedupes by name, most-recent wins.
 */
export function extractEmitting(
  chunks: StreamChunk[],
  _unused?: unknown,
  options: EmittingOptions = {},
): EmittingItem[] {
  const { limit = 5 } = options;

  const LINK_RE = /\[([^\]]*)\]\(([^)]+)\)/g;
  const ARTIFACT_RE = /\/artifacts\//;

  const byName = new Map<string, EmittingItem>();

  for (const chunk of chunks) {
    if (chunk.chunk_type !== "text" || !chunk.content) continue;

    let match: RegExpExecArray | null;
    LINK_RE.lastIndex = 0;
    while ((match = LINK_RE.exec(chunk.content)) !== null) {
      const raw = match[2];
      if (raw.startsWith("http") || raw.startsWith("#")) continue;

      // Strip :lineNumber suffix
      const colonIdx = raw.lastIndexOf(":");
      let path: string = raw;
      if (colonIdx > 0) {
        const maybeNum = raw.slice(colonIdx + 1);
        if (!isNaN(parseInt(maybeNum, 10)) && String(parseInt(maybeNum, 10)) === maybeNum) {
          path = raw.slice(0, colonIdx);
        }
      }

      const namePart = path.split("/").pop() ?? path;
      if (!namePart) continue;

      const kind: "artifact" | "file" = ARTIFACT_RE.test(path) ? "artifact" : "file";
      byName.set(namePart, { name: namePart, path, kind, status: "done" });
    }
  }

  return Array.from(byName.values()).slice(0, limit);
}
