import { describe, it, expect } from "vitest";
import { resolveRunLabel } from "./runLabel";
import type { RunSummary } from "./types";

function makeRun(overrides: Partial<RunSummary> & { run_id: string }): RunSummary {
  return {
    status: "running",
    playbook_name: null,
    agent_name: null,
    invocation_kind: null,
    show_topic: null,
    show_play_name: null,
    started_at: null,
    ...overrides,
  } as RunSummary;
}

describe("resolveRunLabel", () => {
  it("prefers the backend-resolved name over every other field", () => {
    const run = makeRun({
      run_id: "r1",
      name: "implementer · 14:22",
      playbook_name: "pr-merge-review",
      agent_name: "implementer",
    });
    expect(resolveRunLabel(run)).toBe("implementer · 14:22");
  });

  it("falls back to show_play_name when name is absent", () => {
    const run = makeRun({ run_id: "r1", show_play_name: "ADR-0099 rollout", playbook_name: "pb" });
    expect(resolveRunLabel(run)).toBe("ADR-0099 rollout");
  });

  it("falls back to playbook_name when name and show_play_name are absent", () => {
    const run = makeRun({ run_id: "r1", playbook_name: "reviewer" });
    expect(resolveRunLabel(run)).toBe("reviewer");
  });

  it("falls back to agent_name when nothing more structured is available", () => {
    const run = makeRun({ run_id: "r1", agent_name: "implementer" });
    expect(resolveRunLabel(run)).toBe("implementer");
  });

  it("falls back to the last 12 chars of run_id when every field is empty", () => {
    const run = makeRun({ run_id: "0123456789abcdef" });
    expect(resolveRunLabel(run)).toBe("456789abcdef");
  });

  it("treats a blank/whitespace-only name as absent", () => {
    const run = makeRun({ run_id: "r1", name: "   ", playbook_name: "reviewer" });
    expect(resolveRunLabel(run)).toBe("reviewer");
  });

  it("recomputes a backend-resolved agent-role name's HH:MM in local time", () => {
    // 2026-01-01T14:22:00Z — vite.config.mts pins the test process to TZ=UTC,
    // so "local" and UTC agree here and the pinned digits are reproducible.
    const run = makeRun({
      run_id: "r1",
      name: "implementer · 09:00",
      agent_name: "implementer",
      started_at: 1767277320,
    });
    expect(resolveRunLabel(run)).toBe("implementer · 14:22");
  });

  it("leaves a backend-resolved agent-role name alone when started_at is missing", () => {
    const run = makeRun({
      run_id: "r1",
      name: "implementer · 09:00",
      agent_name: "implementer",
      started_at: null,
    });
    expect(resolveRunLabel(run)).toBe("implementer · 09:00");
  });

  it("appends a local HH:MM disambiguator to a bare agent_name fallback", () => {
    // Parity fixture with tests/state/test_session_naming.py's
    // agent_role_label case (started_at 1767277320.0 -> "14:22" in UTC,
    // matching this suite's pinned TZ=UTC local time).
    const run = makeRun({ run_id: "r1", agent_name: "implementer", started_at: 1767277320 });
    expect(resolveRunLabel(run)).toBe("implementer · 14:22");
  });
});
