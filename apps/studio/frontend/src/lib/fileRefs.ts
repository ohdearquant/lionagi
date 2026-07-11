/**
 * Resolve file references found in rendered agent messages (markdown links
 * and bare inline-code filenames) against the run's KNOWN file surface —
 * never fabricates a target from text alone.
 */

const FILENAME_EXTENSIONS =
  "md|markdown|py|pyi|js|jsx|mjs|cjs|ts|tsx|json|jsonl|txt|ya?ml|toml|rs|go|java|kt|" +
  "c|cc|cpp|h|hpp|sh|bash|zsh|css|scss|less|html?|csv|tsv|log|xml|ini|cfg|conf|sql|" +
  "proto|graphql|lock|env|patch|diff|rst|svg";

const FILENAME_RE = new RegExp(`^[A-Za-z0-9_][\\w./-]*\\.(${FILENAME_EXTENSIONS})$`, "i");

/** Conservative heuristic for "this inline-code span looks like a filename" —
 * requires a recognized extension, no whitespace, and a reasonable length. */
export function looksLikeFilename(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed || trimmed.length > 200) return false;
  if (/\s/.test(trimmed)) return false;
  return FILENAME_RE.test(trimmed);
}

/** Strip trailing sentence punctuation a model might glue onto a path
 * ("Wrote review.md." / "see notes.txt,"). */
export function stripTrailingPunctuation(text: string): string {
  return text.replace(/[.,;:!?)\]}'"]+$/, "");
}

export type FileMatch =
  | { type: "none" }
  | { type: "single"; path: string }
  | { type: "ambiguous"; candidates: string[] };

function basename(p: string): string {
  const parts = p.split("/");
  return parts[parts.length - 1] ?? p;
}

export interface ResolveFileRefOptions {
  /** Absolute paths known to belong to this run (files touched by tool calls). */
  knownFiles: string[];
  /** The emitting agent's own artifact subdir, absolute path, checked first. */
  agentDir?: string | null;
}

/**
 * Resolve a raw reference (markdown link target, or a bare filename token)
 * against the known file surface. Absolute refs must match a known file
 * exactly. Bare/relative refs match by basename, preferring candidates
 * under agentDir; multiple remaining matches come back "ambiguous" rather
 * than guessing.
 */
export function resolveFileRef(rawRef: string, opts: ResolveFileRefOptions): FileMatch {
  const ref = stripTrailingPunctuation(rawRef.trim());
  if (!ref) return { type: "none" };

  const knownFiles = opts.knownFiles ?? [];
  const isAbsolute = ref.startsWith("/");

  if (isAbsolute) {
    return knownFiles.includes(ref) ? { type: "single", path: ref } : { type: "none" };
  }

  // file:// URLs — normalize to a bare absolute path before matching.
  if (ref.startsWith("file://")) {
    const stripped = ref.slice("file://".length);
    return knownFiles.includes(stripped) ? { type: "single", path: stripped } : { type: "none" };
  }

  const refBase = basename(ref);
  const candidates = knownFiles.filter((f) => {
    if (f === ref) return true;
    if (f.endsWith(`/${ref}`)) return true;
    return basename(f) === refBase;
  });

  if (candidates.length === 0) return { type: "none" };
  if (candidates.length === 1) return { type: "single", path: candidates[0] };

  if (opts.agentDir) {
    const agentDir = opts.agentDir.endsWith("/") ? opts.agentDir : `${opts.agentDir}/`;
    const inAgentDir = candidates.filter((c) => c.startsWith(agentDir));
    if (inAgentDir.length === 1) return { type: "single", path: inAgentDir[0] };
    if (inAgentDir.length > 1) return { type: "ambiguous", candidates: inAgentDir };
  }

  return { type: "ambiguous", candidates };
}
