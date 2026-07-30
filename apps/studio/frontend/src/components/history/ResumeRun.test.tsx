import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { SessionBranch } from "@/lib/api";

const resumeRunMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  resumeRun: resumeRunMock,
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

  async function mount(branches: SessionBranch[], onResumed = vi.fn()) {
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <ResumeRun runId="run-1" branches={branches} onResumed={onResumed} />
        </IntlProvider>,
      );
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
    const onResumed = vi.fn();
    resumeRunMock.mockResolvedValue({
      run_id: "run-1",
      branch_id: "branch-b",
      invocation_id: "invocation-2",
    });
    await mount([branch("branch-a", "Research"), branch("branch-b", "Review")], onResumed);

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
});
