/**
 * PlaybookTemplateDetail — recent-runs row naming.
 *
 * Every other list/board surface shows a run's resolveRunLabel() output;
 * this list used to show the raw run_id tail instead, so the same run read
 * as two different things depending which surface you looked at it from.
 * Runs launched from one playbook also mostly share that playbook's name
 * tier, so resolveRunLabel() alone collapses distinct rows to one label —
 * the short id stays visible as a muted secondary so rows are still
 * distinguishable at a glance.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { RunSummary } from "@/lib/types";
import { ToastProvider } from "@/components/ui/Toast";

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, className }: { children?: React.ReactNode; className?: string }) => (
    <div className={className}>{children}</div>
  ),
}));

const api = vi.hoisted(() => ({
  getBuiltinPlaybookRaw: vi.fn(),
  getWorkerRaw: vi.fn(),
  installBuiltinPlaybook: vi.fn(),
  launchPlaybook: vi.fn(),
  listRuns: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, ...api };
});

const { PlaybookTemplateDetail } = await import("./PlaybookTemplateDetail");

function run(overrides: Partial<RunSummary> & { run_id: string }): RunSummary {
  return {
    status: "completed",
    started_at: 100,
    ended_at: 160,
    playbook_name: "pr-merge-review",
    agent_name: null,
    ...overrides,
  } as RunSummary;
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("PlaybookTemplateDetail — recent runs row", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    Object.values(api).forEach((fn) => fn.mockReset());
    api.getBuiltinPlaybookRaw.mockResolvedValue({ data: { description: "reviews a PR" } });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function mount(runs: RunSummary[]) {
    api.listRuns.mockResolvedValue({
      runs,
      page: 1,
      per_page: 10,
      total: runs.length,
      total_pages: 1,
      has_next: false,
      has_prev: false,
    });
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <ToastProvider>
            <PlaybookTemplateDetail name="pr-merge-review" isBuiltin />
          </ToastProvider>
        </IntlProvider>,
      );
    });
    await flush();
  }

  it("shows the resolved run label as the primary text, not the raw run_id tail", async () => {
    await mount([run({ run_id: "run-abcdef0123456789", show_play_name: "ADR-0099 rollout" })]);
    const primary = container.querySelector(".flex-1.truncate");
    expect(primary?.textContent).toBe("ADR-0099 rollout");
  });

  it("keeps the short id visible as a muted secondary alongside the resolved label", async () => {
    // Both runs share the same playbook_name tier, so resolveRunLabel()
    // alone would render two identical rows -- the short id is what keeps
    // them distinguishable.
    await mount([run({ run_id: "run-aaaaaaaaaaaaaaaa" }), run({ run_id: "run-bbbbbbbbbbbbbbbb" })]);
    expect(container.textContent).toContain("pr-merge-review");
    expect(container.textContent).toContain("aaaaaaaaaaaa");
    expect(container.textContent).toContain("bbbbbbbbbbbb");
  });
});
