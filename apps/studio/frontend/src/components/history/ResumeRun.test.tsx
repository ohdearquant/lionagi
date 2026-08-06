import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { SessionBranch } from "@/lib/api";
import type { RunResumeResponse } from "@/lib/types";

const resumeRunMock = vi.hoisted(() => vi.fn());
const getResumeAvailabilityMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  resumeRun: resumeRunMock,
  getResumeAvailability: getResumeAvailabilityMock,
}));

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, search }: { children?: React.ReactNode; search?: Record<string, string> }) => (
    <a href={`/fleet?${new URLSearchParams(search).toString()}`}>{children}</a>
  ),
}));

const { default: ResumeRun, resumeCommand, shellQuote } = await import("./ResumeRun");

function branch(id: string, name: string): SessionBranch {
  return {
    id,
    name,
    created_at: 1,
    messages: [],
    model: "claude/sonnet",
  };
}

describe("ResumeRun helpers", () => {
  it("builds a shell-safe copy escape hatch", () => {
    expect(shellQuote("reviewer's branch")).toBe("'reviewer'\\''s branch'");
    expect(resumeCommand("branch-1", "check 'again'")).toBe(
      "li agent -r 'branch-1' --prompt 'check '\\''again'\\'''",
    );
    expect(resumeCommand(null, "")).toBe("li agent -r <branch-id> --prompt 'follow-up'");
  });
});

describe("ResumeRun component", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    resumeRunMock.mockReset();
    getResumeAvailabilityMock.mockReset();
    getResumeAvailabilityMock.mockResolvedValue({
      run_id: "run-1",
      invocation_kind: "agent",
      resumable: true,
      branch_id: "branch-a",
    });
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function mount(
    branches: SessionBranch[],
    opts: {
      invocationKind?: string | null;
      onResumed?: (result: RunResumeResponse) => void;
    } = {},
  ) {
    const onResumed = opts.onResumed ?? vi.fn();
    const invocationKind = opts.invocationKind ?? "agent";
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <ResumeRun
            runId="run-1"
            invocationKind={invocationKind}
            branches={branches}
            onResumed={onResumed}
          />
        </IntlProvider>,
      );
      // Let the resumability precheck's promise chain resolve and its state
      // update commit before assertions run.
      await Promise.resolve();
      await Promise.resolve();
    });
    return onResumed;
  }

  it("requires an explicit branch for a multi-branch run and always shows CLI fallback", async () => {
    await mount([branch("branch-a", "Research"), branch("branch-b", "Review")]);

    expect(container.querySelector("select")).not.toBeNull();
    expect(container.textContent).toContain("CLI escape hatch");
    expect(container.querySelector("code")?.textContent).toContain("<branch-id>");
    const resume = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("Resume"),
    );
    expect(resume?.disabled).toBe(true);
  });

  it("posts the selected branch and keeps a linked accepted state", async () => {
    resumeRunMock.mockResolvedValue({
      run_id: "run-1",
      branch_id: "branch-b",
      invocation_id: "invocation-2",
    });
    const onResumed = await mount([branch("branch-a", "Research"), branch("branch-b", "Review")]);

    const select = container.querySelector("select")!;
    const textarea = container.querySelector("textarea")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set?.call(
        select,
        "branch-b",
      );
      select.dispatchEvent(new Event("change", { bubbles: true }));
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(
        textarea,
        "Review the final patch",
      );
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const resume = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("Resume"),
    )!;
    await act(async () => {
      resume.click();
    });

    expect(resumeRunMock).toHaveBeenCalledWith("run-1", {
      instruction: "Review the final patch",
      branch_id: "branch-b",
    });
    expect(onResumed).toHaveBeenCalled();
    expect(container.textContent).toContain("Follow-up accepted");
    expect(container.textContent).toContain("invocation-2");
    expect(container.querySelector("a")?.getAttribute("href")).toBe(
      "/fleet?s=run-1&invocation=invocation-2",
    );
  });

  it("shows the checking state before the resumability precheck resolves", async () => {
    let resolveAvailability!: (value: unknown) => void;
    getResumeAvailabilityMock.mockReturnValue(
      new Promise((resolve) => {
        resolveAvailability = resolve;
      }),
    );

    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <ResumeRun runId="run-1" invocationKind="agent" branches={[]} onResumed={vi.fn()} />
        </IntlProvider>,
      );
    });

    expect(container.textContent).toContain("Checking whether this run can be resumed");
    expect(container.querySelector("textarea")).toBeNull();

    await act(async () => {
      resolveAvailability({
        run_id: "run-1",
        invocation_kind: "agent",
        resumable: true,
      });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(container.querySelector("textarea")).not.toBeNull();
  });

  it("still offers the branch picker when an agent run's only obstacle is choosing a branch", async () => {
    // branch_conflict means the backend could not pick a branch by itself, not
    // that the run cannot be resumed — the form below renders a selector for
    // exactly this case. Treating it as unresumable made multi-branch agent
    // resume unreachable from the UI even though the API still accepted it.
    getResumeAvailabilityMock.mockResolvedValue({
      run_id: "run-1",
      invocation_kind: "agent",
      resumable: false,
      reason: "branch_conflict",
      message: "Run 'run-1' does not resolve to exactly one resumable branch.",
    });

    await mount(
      [
        { id: "branch-a", name: "worker-a" },
        { id: "branch-b", name: "worker-b" },
      ] as SessionBranch[],
      { invocationKind: "agent" },
    );

    // The picker and the instruction box are both present.
    expect(container.querySelector("select")).not.toBeNull();
    expect(container.querySelector("textarea")).not.toBeNull();
  });

  it("still refuses a branch_conflict that offers no branch to choose between", async () => {
    // The bypass is guarded on there being a real choice; without one the
    // explained refusal is still the right surface.
    getResumeAvailabilityMock.mockResolvedValue({
      run_id: "run-1",
      invocation_kind: "agent",
      resumable: false,
      reason: "branch_conflict",
      message: "Run 'run-1' does not resolve to exactly one resumable branch.",
    });

    await mount([], { invocationKind: "agent" });

    expect(container.textContent).toContain("does not resolve to exactly one resumable branch");
    expect(container.querySelector("textarea")).toBeNull();
  });

  it("renders an explicit explanation, not a dead control, when the run is not resumable", async () => {
    getResumeAvailabilityMock.mockResolvedValue({
      run_id: "run-1",
      invocation_kind: "flow",
      resumable: false,
      reason: "no_checkpoint",
      message: "No checkpoint.json found for run 'cli-run-1'.",
    });

    await mount([], { invocationKind: "flow" });

    expect(container.textContent).toContain("No checkpoint.json found for run 'cli-run-1'.");
    expect(container.querySelector("button")).toBeNull();
    expect(container.querySelector("textarea")).toBeNull();
  });

  for (const kind of ["play", "flow", "show-play", "fanout"]) {
    it(`renders a no-branch, no-instruction continue action for invocation_kind=${kind}`, async () => {
      getResumeAvailabilityMock.mockResolvedValue({
        run_id: "run-1",
        invocation_kind: kind,
        resumable: true,
        checkpoint_run_id: "cli-run-1",
      });
      resumeRunMock.mockResolvedValue({
        run_id: "run-1",
        invocation_kind: kind,
        invocation_id: "invocation-flow-1",
        checkpoint_run_id: "cli-run-1",
      });

      await mount([], { invocationKind: kind });

      expect(container.querySelector("textarea")).toBeNull();
      expect(container.querySelector("select")).toBeNull();
      expect(container.textContent).toContain("Replays the saved plan from where it stopped");

      const submit = [...container.querySelectorAll("button")].find((button) =>
        button.textContent?.includes("Continue"),
      )!;
      await act(async () => {
        submit.click();
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(resumeRunMock).toHaveBeenCalledWith("run-1", {});
      expect(container.textContent).toContain("Follow-up accepted");
    });
  }
});
