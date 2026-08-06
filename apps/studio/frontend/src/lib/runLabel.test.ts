import { describe, it, expect } from "vitest";
import { runLabel } from "./runLabel";

const RUN_ID = "20260806T122729-daa54a";

describe("runLabel", () => {
  it("uses the renamed display_name over the per-field chain", () => {
    // The whole point: a renamed session must read the same in a list as it
    // does in the run header.
    expect(
      runLabel({
        run_id: RUN_ID,
        display_name: "Adobe demo run",
        playbook_name: "fix-execgraph",
        agent_name: "claude-code",
      }),
    ).toBe("Adobe demo run");
  });

  it("treats a blank display_name as absent rather than as a label", () => {
    // Load-bearing: the backend resolver's last resort returns "" (not null)
    // for a row carrying no id, and `"" ?? fallback` keeps the empty string.
    // Without this the label would go blank for those rows.
    for (const blank of ["", "   ", "\t"]) {
      expect(runLabel({ run_id: RUN_ID, display_name: blank, playbook_name: "pb" })).toBe("pb");
    }
  });

  it("trims a display_name that carries surrounding whitespace", () => {
    expect(runLabel({ run_id: RUN_ID, display_name: "  spaced  " })).toBe("spaced");
  });

  it("falls back to the old chain when display_name is missing entirely", () => {
    // A frontend talking to a daemon that predates display_name must be
    // byte-for-byte unchanged.
    expect(runLabel({ run_id: RUN_ID, playbook_name: "pb", agent_name: "ag" })).toBe("pb");
    expect(runLabel({ run_id: RUN_ID, agent_name: "ag" })).toBe("ag");
    expect(runLabel({ run_id: RUN_ID })).toBe(RUN_ID.slice(-12));
  });

  it("honours a caller's shorter id tail", () => {
    expect(runLabel({ run_id: RUN_ID }, 8)).toBe(RUN_ID.slice(-8));
  });

  it("does not let a null display_name shadow a real name", () => {
    expect(runLabel({ run_id: RUN_ID, display_name: null, agent_name: "ag" })).toBe("ag");
  });
});
