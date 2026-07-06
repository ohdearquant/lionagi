/**
 * Classifies a run's raw error_detail into one human line — never render a
 * traceback tail in a list or summary. Python tracebacks always end with
 * "ExceptionType: message", so the last non-empty line is the fallback when
 * no known pattern matches; the full text stays available for expansion.
 */
type Translate = (key: string) => string;

const PATTERNS: Array<{ re: RegExp; key: string }> = [
  { re: /failed to spawn/i, key: "spawnFailed" },
  { re: /econnrefused|connection refused|connectionerror|network is unreachable/i, key: "network" },
  { re: /timed out|timeouterror/i, key: "timeout" },
  { re: /permissionerror|permission denied/i, key: "permission" },
  { re: /modulenotfounderror|importerror/i, key: "missingDependency" },
  { re: /filenotfounderror|no such file or directory/i, key: "notFound" },
];

const MAX_LEN = 100;

function lastMeaningfulLine(detail: string): string {
  const lines = detail
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  return lines[lines.length - 1] ?? detail.trim();
}

/** One-line classification of a run's error_detail; null when there is none. */
export function classifyError(detail: string | null | undefined, t: Translate): string | null {
  if (!detail || !detail.trim()) return null;
  for (const { re, key } of PATTERNS) {
    if (re.test(detail)) return t(key);
  }
  const last = lastMeaningfulLine(detail);
  return last.length > MAX_LEN ? `${last.slice(0, MAX_LEN)}…` : last;
}
