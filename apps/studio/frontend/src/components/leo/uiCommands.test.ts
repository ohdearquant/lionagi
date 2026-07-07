import { describe, it, expect, vi } from "vitest";
import { applyUiCommand, describeUiCommand, uiCommandSearch } from "./uiCommands";
import type { LeoUiCommand } from "@/lib/api";

describe("uiCommandSearch", () => {
  it("preserves status, playbook, project, tab, and sel", () => {
    expect(
      uiCommandSearch({
        status: "running",
        playbook: "daily",
        project: "ocean",
        tab: "skill",
        sel: "agent:reviewer",
      }),
    ).toEqual({
      status: "running",
      playbook: "daily",
      project: "ocean",
      tab: "skill",
      sel: "agent:reviewer",
    });
  });

  it("drops empty string params", () => {
    expect(uiCommandSearch({ status: "", tab: "skill" })).toEqual({ tab: "skill" });
  });

  it("returns an empty object when params is undefined", () => {
    expect(uiCommandSearch(undefined)).toEqual({});
  });
});

describe("applyUiCommand — navigate", () => {
  it("navigates to /schedules with the schedule id, even called repeatedly", () => {
    const navigate = vi.fn();
    const cmd: LeoUiCommand = { kind: "navigate", space: "schedules", params: { s: "sched-1" } };

    applyUiCommand(cmd, navigate);
    expect(navigate).toHaveBeenCalledWith({ to: "/schedules", search: { s: "sched-1" } });

    // A second navigate call to the same route with a new id must still be
    // issued — this is what lets an already-mounted /schedules page pick up
    // a later deep link instead of only opening on first mount.
    const cmd2: LeoUiCommand = { kind: "navigate", space: "schedules", params: { s: "sched-2" } };
    applyUiCommand(cmd2, navigate);
    expect(navigate).toHaveBeenLastCalledWith({ to: "/schedules", search: { s: "sched-2" } });
    expect(navigate).toHaveBeenCalledTimes(2);
  });

  it("preserves status, playbook, project, tab, and sel for other spaces", () => {
    const navigate = vi.fn();
    const cmd: LeoUiCommand = {
      kind: "navigate",
      space: "fleet",
      params: { status: "running", playbook: "daily", project: "ocean" },
    };
    applyUiCommand(cmd, navigate);
    expect(navigate).toHaveBeenCalledWith({
      to: "/fleet",
      search: { status: "running", playbook: "daily", project: "ocean" },
    });
  });

  it("returns null and does not navigate for an unknown space", () => {
    const navigate = vi.fn();
    const result = applyUiCommand({ kind: "navigate", space: "nowhere" }, navigate);
    expect(result).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe("applyUiCommand — prefill_schedule", () => {
  it("produces create/name/cron/prompt/desc search and opens the create form", () => {
    const navigate = vi.fn();
    const label = applyUiCommand(
      {
        kind: "prefill_schedule",
        params: { name: "Nightly digest", cron: "0 6 * * *", prompt: "summarize", desc: "daily" },
      },
      navigate,
    );
    expect(navigate).toHaveBeenCalledWith({
      to: "/schedules",
      search: {
        create: "1",
        name: "Nightly digest",
        cron: "0 6 * * *",
        prompt: "summarize",
        desc: "daily",
      },
    });
    expect(label).toBe('schedules · new "Nightly digest"');
  });
});

describe("applyUiCommand — unrecognized kind", () => {
  it("returns null without navigating", () => {
    const navigate = vi.fn();
    expect(applyUiCommand({ kind: "unknown_kind" }, navigate)).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe("describeUiCommand", () => {
  it("includes the space when present", () => {
    expect(describeUiCommand({ kind: "navigate", space: "fleet" })).toBe("navigate → fleet");
  });

  it("falls back to just the kind when there is no space", () => {
    expect(describeUiCommand({ kind: "prefill_schedule" })).toBe("prefill_schedule");
  });
});
