/**
 * The Files panel lists files, not every slash-bearing word in a command.
 *
 * The extractor used to accept any token containing a slash, or any token
 * whose basename contained a dot. Command arguments are full of prose that
 * passes both: DOIs, throughput units, ratios, and slash-separated lists of
 * product names. Measured over five runs of four different agent shapes and
 * ~800 shell commands, that rule produced 769 "files". Accepting a rooted
 * path outright and asking a rootless one to look like a filename produced
 * 139, and dropped nothing that existed on disk.
 *
 * Rootless paths are kept deliberately: they resolve against the emitting
 * agent's artifact directory, so `analyst/disposition_table.md` is a real
 * reference even though it names no root.
 *
 * The cases below are verbatim from that measurement, so a future loosening
 * of the rule fails here rather than on the demo screen.
 */
import { describe, expect, it } from "vitest";
import { pathFromArgs } from "./RunStepCard";

function paths(command: string): string[] {
  return pathFromArgs({ command }, "", "bash");
}

describe("shell file extraction", () => {
  it("keeps rooted paths, including the ones a command only reads", () => {
    expect(paths("cat /Users/dev/projects/docs/INDEX.md")).toEqual([
      "/Users/dev/projects/docs/INDEX.md",
    ]);
    expect(paths("uv run pytest ~/work/tests/test_flow.py")).toEqual(["~/work/tests/test_flow.py"]);
    // `./` and `../` are roots too; they normalise away, which is expected.
    expect(paths("ruff check ./src/lionagi/config.py")).toEqual(["src/lionagi/config.py"]);
  });

  it("keeps a rootless path that names a real file, since it resolves against the agent dir", () => {
    expect(paths("cat analyst/disposition_table.md")).toEqual(["analyst/disposition_table.md"]);
    expect(paths("cat crates/khive-db/src/writer_task.rs")).toEqual([
      "crates/khive-db/src/writer_task.rs",
    ]);
  });

  it("rejects a rootless dotted word that is not a filename", () => {
    // Only the extension gate separates these from the case above: they are
    // rootless, they have a dot, and the old rule took both as files.
    expect(paths("echo sqlite.org")).toEqual([]);
    expect(paths("echo read.transaction")).toEqual([]);
  });

  it("rejects prose that merely contains a slash", () => {
    // Every one of these appeared in the Files panel of a real run.
    for (const prose of [
      "echo 10.1145/502059.502057",
      "echo Kingman/GI/G/1",
      "echo Litestream/LiteFS/libSQL/rqlite/Bedrock",
      "echo MB/s",
      "echo I/O",
      "echo 15/20",
      "echo FULL/RESTART/TRUNCATE",
      "echo admission/bounded",
    ]) {
      expect(paths(prose), `${prose} produced a file`).toEqual([]);
    }
  });

  it("rejects a wrapped script captured as one quoted token", () => {
    // The tokenizer returns the body of a quoted argument as a single word.
    // Before the root and segment rules, this whole script was one "file".
    const wrapped = `/bin/zsh -lc '/usr/bin/find /Users/dev -name FINDINGS.md -mmin -60'`;
    expect(paths(wrapped)).not.toContain("/usr/bin/find /Users/dev -name FINDINGS.md -mmin -60");
  });

  it("claims nothing at all for a path only visible inside a quoted wrapper", () => {
    // A known limit, asserted so it stays known. The tokenizer cannot see
    // inside a quoted argument, so the real path here is invisible. Listing
    // nothing is the correct outcome; listing the script was the defect.
    const wrapped = `/bin/zsh -lc "cat /Users/dev/projects/notes/a.md"`;
    expect(paths(wrapped)).toEqual([]);
  });

  it("keeps paths whose directories are named in a non-Latin script", () => {
    // The segment check ran before the rooted check, so an ASCII-only class
    // rejected these before the rooted rule could vouch for them. Wherever
    // directories are routinely named outside Latin script, that dropped real
    // files. Rooted and rootless both, since widening the class fixes
    // the rootless case too as long as the name still looks like a file.
    expect(paths("cat /Users/dev/项目/main.py")).toEqual(["/Users/dev/项目/main.py"]);
    expect(paths("cat /Users/dev/Café/notes.md")).toEqual(["/Users/dev/Café/notes.md"]);
    expect(paths("cat 文档/报告.md")).toEqual(["文档/报告.md"]);
  });

  it("keeps the punctuation a filename is actually allowed to carry", () => {
    // The charset edges, so a future tightening of the class fails here
    // rather than silently dropping rows. Each of these characters is in the
    // segment class on purpose.
    expect(paths("cat /tmp/run-2026_08@v1.2+build~1/out.log")).toEqual([
      "/tmp/run-2026_08@v1.2+build~1/out.log",
    ]);
  });

  it("still rejects the punctuation that means the token is not a path", () => {
    // The control for the two cases above: widening the class to every script
    // must not dissolve it. Without this, replacing the class with something
    // that matches anything passes both keep-arms.
    for (const frag of ["run a=b/c.py", "echo 50%/hour", "note see p.12,/x.md"]) {
      expect(paths(frag)).toEqual([]);
    }
  });

  it("rejects regex and shell fragments", () => {
    for (const frag of [
      `rg "\\.busy\\b" /Users/dev/projects`,
      `rg "read.transaction" .`,
      `awk '/^##/ {print}' /Users/dev/x.md`,
    ]) {
      for (const p of paths(frag)) {
        expect(p.startsWith("/"), `${p} is not rooted`).toBe(true);
      }
    }
  });
});
