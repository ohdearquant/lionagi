/**
 * The Errors panel used to count only tool calls that came back with an error,
 * while its copy claimed "no errors detected across all branches". A branch
 * that dies before it calls anything makes zero tool errors, so the panel read
 * clean for the worst outcome a branch can have. These tests are about the two
 * axes staying separate and both being counted.
 */
import { describe, expect, it } from "vitest";
import { collectBranchFailures, isFailedStatus, resolveOverviewCounts } from "./RunDetail";
import type { SessionBranch } from "@/lib/api";

function branch(over: Partial<SessionBranch> & { id: string }): SessionBranch {
  return {
    name: "",
    created_at: 0,
    messages: [],
    ...over,
  } as SessionBranch;
}

describe("isFailedStatus", () => {
  it("accepts the state layer's terminal failure vocabulary", () => {
    for (const s of ["failed", "timed_out", "aborted"]) {
      expect(isFailedStatus(s), s).toBe(true);
    }
  });

  // Both arms matter here. Dropping a status from the failure set is only
  // correct if the genuine failures above still register, so these two tests
  // have to pass together: a change that simply stops flagging things would
  // satisfy this one alone.
  it("does not treat an escalation as a failure", () => {
    expect(isFailedStatus("escalated")).toBe(false);
  });

  it("is case- and whitespace-insensitive, since the status is a free string", () => {
    expect(isFailedStatus("  FAILED ")).toBe(true);
    expect(isFailedStatus("Timed_Out")).toBe(true);
  });

  it("does not treat a deliberate stop or a success as an error", () => {
    for (const s of ["completed", "completed_empty", "cancelled", "running", ""]) {
      expect(isFailedStatus(s), s).toBe(false);
    }
  });

  it("treats a missing status as not-a-failure rather than throwing", () => {
    expect(isFailedStatus(null)).toBe(false);
    expect(isFailedStatus(undefined)).toBe(false);
  });
});

describe("collectBranchFailures", () => {
  it("reports a branch that failed with no messages at all", () => {
    // The case the tool-call scan is blind to, and the reason this exists.
    const out = collectBranchFailures([branch({ id: "abcdef123456", status: "failed" })]);
    expect(out).toHaveLength(1);
    expect(out[0].kind).toBe("branch");
    expect(out[0].output).toBe("failed");
  });

  it("names the branch, falling back to a short id when it is unnamed", () => {
    const out = collectBranchFailures([
      branch({ id: "abcdef123456", name: "researcher", status: "failed" }),
      branch({ id: "0123456789ab", name: "", status: "aborted" }),
    ]);
    expect(out.map((e) => e.branch)).toEqual(["researcher", "01234567"]);
  });

  it("emits exactly one entry per failed branch, never per message or segment", () => {
    const out = collectBranchFailures([
      branch({
        id: "a".repeat(12),
        name: "worker",
        status: "failed",
        messages: [{}, {}, {}] as SessionBranch["messages"],
      }),
    ]);
    expect(out).toHaveLength(1);
  });

  it("ignores branches with no status of their own", () => {
    // buildRunSteps substitutes the SESSION status for a branch that carries
    // none. Reading that back would report every branch of a failed run as
    // individually failed, which is the over-count in the other direction.
    const out = collectBranchFailures([
      branch({ id: "a".repeat(12), status: null }),
      branch({ id: "b".repeat(12) }),
    ]);
    expect(out).toEqual([]);
  });

  it("returns nothing for a healthy run, and survives no branches at all", () => {
    expect(collectBranchFailures([branch({ id: "a".repeat(12), status: "completed" })])).toEqual(
      [],
    );
    expect(collectBranchFailures([])).toEqual([]);
    expect(collectBranchFailures(undefined)).toEqual([]);
  });
});

describe("resolveOverviewCounts", () => {
  it("adds branch failures to the server's tool-error count", () => {
    // The server's error_count is its own scan of tool calls, so the two
    // numbers count disjoint things and adding them is not double-counting.
    const { errorCount } = resolveOverviewCounts(
      { tool_call_count: 40, error_count: 2 } as never,
      { toolCallCount: 12, errorCount: 1 },
      3,
    );
    expect(errorCount).toBe(5);
  });

  it("adds branch failures to the loaded count when the server has no stats", () => {
    const { errorCount } = resolveOverviewCounts(
      undefined,
      {
        toolCallCount: 12,
        errorCount: 1,
      },
      2,
    );
    expect(errorCount).toBe(3);
  });

  it("reports failures even when nothing errored at the tool level", () => {
    // The screen that started this: two failed branches, zero tool errors,
    // and an overview reading 0.
    const { errorCount } = resolveOverviewCounts(
      { tool_call_count: 0, error_count: 0 } as never,
      { toolCallCount: 0, errorCount: 0 },
      2,
    );
    expect(errorCount).toBe(2);
  });

  it("still reports zero for a run with neither kind of failure", () => {
    const { errorCount } = resolveOverviewCounts(
      { tool_call_count: 40, error_count: 0 } as never,
      { toolCallCount: 40, errorCount: 0 },
      0,
    );
    expect(errorCount).toBe(0);
  });

  it("leaves the tool-call count alone", () => {
    const { toolCallCount } = resolveOverviewCounts(
      { tool_call_count: 40, error_count: 0 } as never,
      { toolCallCount: 12, errorCount: 0 },
      3,
    );
    expect(toolCallCount).toBe(40);
  });
});
