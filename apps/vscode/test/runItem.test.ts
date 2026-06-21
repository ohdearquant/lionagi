/**
 * Unit tests for hasRunTree (src/runs/runItem.ts) — the predicate that gates the
 * inline "View Run Tree" button. A run only earns the button when it has sub-nodes:
 * a multi-agent invocation kind, or any run that spawned more than one branch.
 */
import { describe, it, expect } from "vitest";
import { hasRunTree } from "../src/runs/runItem.js";
import type { Run } from "../src/api/types.js";

function run(overrides: Partial<Run>): Run {
  return {
    run_id: "r1",
    id: "s1",
    name: null,
    playbook_name: null,
    agent_name: null,
    invocation_kind: "agent",
    model: null,
    provider: null,
    effort: null,
    status: "completed",
    started_at: null,
    ended_at: null,
    created_at: 0,
    updated_at: null,
    last_message_at: null,
    effective_health: null,
    branch_count: 1,
    message_count: 0,
    project: null,
    project_source: null,
    invocation_id: null,
    ...overrides,
  };
}

describe("hasRunTree", () => {
  it("is true for multi-agent invocation kinds even with a single branch", () => {
    for (const kind of ["flow", "fanout", "show-play"]) {
      expect(hasRunTree(run({ invocation_kind: kind, branch_count: 1 }))).toBe(true);
    }
  });

  it("is false for single-agent and observed runs with one branch", () => {
    expect(hasRunTree(run({ invocation_kind: "agent", branch_count: 1 }))).toBe(false);
    expect(hasRunTree(run({ invocation_kind: "play", branch_count: 1 }))).toBe(false);
    expect(hasRunTree(run({ invocation_kind: null, branch_count: 1 }))).toBe(false);
  });

  it("is true for any run that spawned more than one branch", () => {
    expect(hasRunTree(run({ invocation_kind: "agent", branch_count: 3 }))).toBe(true);
    expect(hasRunTree(run({ invocation_kind: null, branch_count: 2 }))).toBe(true);
  });

  it("is false when branch_count is zero or missing", () => {
    expect(hasRunTree(run({ invocation_kind: "agent", branch_count: 0 }))).toBe(false);
    expect(
      hasRunTree(run({ invocation_kind: null, branch_count: undefined as unknown as number }))
    ).toBe(false);
  });
});
