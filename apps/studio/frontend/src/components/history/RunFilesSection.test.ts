import { describe, expect, it } from "vitest";
import { groupRunFiles } from "./RunFilesSection";

const ROOT = "/Users/x/khive-work/shows/topic/play";

describe("groupRunFiles", () => {
  it("groups in-root files by their first segment — one group per agent dir", () => {
    const groups = groupRunFiles(
      [
        `${ROOT}/coordinator/preflight.md`,
        `${ROOT}/explorer-2/brief.md`,
        `${ROOT}/coordinator/manifest.md`,
      ],
      ROOT,
    );
    expect(groups.map((g) => g.label)).toEqual(["coordinator", "explorer-2"]);
    expect(groups[0].files.map((f) => f.name)).toEqual(["manifest.md", "preflight.md"]);
    expect(groups.every((g) => g.files.every((f) => f.openable))).toBe(true);
  });

  it("puts a root-level file in the root group, named by its own name", () => {
    const groups = groupRunFiles([`${ROOT}/REPORT.md`], ROOT);
    expect(groups).toHaveLength(1);
    expect(groups[0].label).toBe("·");
    expect(groups[0].files[0]).toEqual({
      path: `${ROOT}/REPORT.md`,
      name: "REPORT.md",
      openable: true,
    });
  });

  it("marks files outside the artifact root not openable — the backend would refuse them", () => {
    const groups = groupRunFiles(["/Users/x/projects/repo/src/main.py"], ROOT);
    expect(groups).toHaveLength(1);
    expect(groups[0].openable).toBe(false);
    expect(groups[0].files[0].openable).toBe(false);
    // Home prefix shortened so a long path reads as a location, not a wall.
    expect(groups[0].label).toBe("~/projects/repo/src");
  });

  it("with no artifact root at all, nothing is openable", () => {
    const groups = groupRunFiles([`${ROOT}/coordinator/preflight.md`], null);
    expect(groups.every((g) => !g.openable)).toBe(true);
  });

  it("normalizes a trailing slash on the root — the same file must not flip between groups", () => {
    const withSlash = groupRunFiles([`${ROOT}/agent/a.md`], `${ROOT}/`);
    const without = groupRunFiles([`${ROOT}/agent/a.md`], ROOT);
    expect(withSlash).toEqual(without);
    expect(withSlash[0].openable).toBe(true);
  });

  it("orders readable groups before provenance-only groups", () => {
    const groups = groupRunFiles(["/elsewhere/z.txt", `${ROOT}/agent/a.md`], ROOT);
    expect(groups.map((g) => g.openable)).toEqual([true, false]);
  });

  it("a sibling directory sharing the root's prefix is not inside the root", () => {
    // `${ROOT}-backup/...` startsWith(ROOT) — the separator check must reject it.
    const groups = groupRunFiles([`${ROOT}-backup/agent/a.md`], ROOT);
    expect(groups[0].openable).toBe(false);
  });
});
