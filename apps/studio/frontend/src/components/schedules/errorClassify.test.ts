/**
 * classifyError — never render a raw traceback tail in a list/summary.
 * Known exception patterns map to a friendly one-liner; anything else falls
 * back to the traceback's own last line (Python conventionally ends a
 * traceback with "ExceptionType: message"), truncated if very long.
 */
import { describe, it, expect } from "vitest";
import { classifyError } from "./errorClassify";

const t = (key: string) => `error.${key}`;

describe("classifyError — null/empty input", () => {
  it("returns null for null", () => {
    expect(classifyError(null, t)).toBeNull();
  });

  it("returns null for undefined", () => {
    expect(classifyError(undefined, t)).toBeNull();
  });

  it("returns null for an empty/whitespace-only string", () => {
    expect(classifyError("   \n  ", t)).toBeNull();
  });
});

describe("classifyError — known patterns", () => {
  it("classifies a spawn failure regardless of surrounding traceback noise", () => {
    const detail = [
      'File "ts.py", line 649, in run_until_complete',
      "    return future.result()",
      "RuntimeError: Failed to spawn: li — daemon CWD is not the project root",
    ].join("\n");
    expect(classifyError(detail, t)).toBe("error.spawnFailed");
  });

  it("classifies a connection refused error", () => {
    expect(classifyError("ConnectionError: [Errno 61] ECONNREFUSED", t)).toBe("error.network");
  });

  it("classifies a timeout", () => {
    expect(classifyError("asyncio.exceptions.TimeoutError", t)).toBe("error.timeout");
  });

  it("classifies a permission error", () => {
    expect(classifyError("PermissionError: [Errno 13] Permission denied: '/root'", t)).toBe(
      "error.permission",
    );
  });

  it("classifies a missing module", () => {
    expect(classifyError("ModuleNotFoundError: No module named 'foo'", t)).toBe(
      "error.missingDependency",
    );
  });

  it("classifies a missing file", () => {
    expect(classifyError("FileNotFoundError: No such file or directory: 'x.yaml'", t)).toBe(
      "error.notFound",
    );
  });

  it("pattern matching is case-insensitive", () => {
    expect(classifyError("failed to spawn subprocess", t)).toBe("error.spawnFailed");
  });
});

describe("classifyError — fallback to the traceback's last line", () => {
  it("takes the last non-empty line, not the noisy frame lines above it", () => {
    const detail = [
      "Traceback (most recent call last):",
      '  File "engine.py", line 12, in run',
      "    do_thing()",
      "SomeCustomError: unexpected state in scheduler",
    ].join("\n");
    expect(classifyError(detail, t)).toBe("SomeCustomError: unexpected state in scheduler");
  });

  it("a single-line detail with no pattern match returns the line itself", () => {
    expect(classifyError("boom", t)).toBe("boom");
  });

  it("truncates a very long fallback line", () => {
    const long = "X".repeat(150);
    const result = classifyError(long, t);
    expect(result).not.toBeNull();
    expect(result!.length).toBeLessThanOrEqual(101);
    expect(result!.endsWith("…")).toBe(true);
  });

  it("ignores trailing blank lines when finding the last meaningful line", () => {
    const detail = "SomeError: boom\n\n\n";
    expect(classifyError(detail, t)).toBe("SomeError: boom");
  });
});
