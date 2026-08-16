/**
 * The detail panel renders lifecycle audit fields that the enable/disable
 * toggle itself changes, so the two have to stay in step. Rendered rather
 * than source-asserted, because the failure this guards is a stale render:
 * every call the component makes can be correct and the panel still show
 * the values it loaded at mount.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import ScheduleDetailModal from "./ScheduleDetailModal";
import { ToastProvider } from "@/components/ui/Toast";
import enMessages from "@/messages/en.json";
import type { ScheduleDetail, ScheduleLifecycleChange } from "@/lib/types";

const api = vi.hoisted(() => ({
  getSchedule: vi.fn(),
  listScheduleRuns: vi.fn(() => Promise.resolve({ runs: [] })),
  updateSchedule: vi.fn(() => Promise.resolve(undefined)),
  deleteSchedule: vi.fn(() => Promise.resolve(undefined)),
  triggerSchedule: vi.fn(() => Promise.resolve({ run_id: "run-abcdefgh" })),
  getInvocation: vi.fn(() => Promise.resolve(null)),
  enableSchedule: vi.fn(() => Promise.resolve(undefined)),
  disableSchedule: vi.fn(() => Promise.resolve(undefined)),
}));
vi.mock("@/lib/api", () => api);
vi.mock("@tanstack/react-router", () => ({ useNavigate: () => vi.fn() }));

function lifecycle(overrides: Partial<ScheduleLifecycleChange> = {}): ScheduleLifecycleChange {
  return {
    id: "lc-1",
    entity_type: "schedule",
    entity_id: "sched-1",
    previous_status: null,
    status: "disabled",
    reason_code: "schedule.disabled.request",
    reason_summary: "paused while the runner was down",
    source: "user",
    actor: "operator",
    created_at: 1_760_000_000_000,
    metadata: null,
    ...overrides,
  };
}

function detail(overrides: Partial<ScheduleDetail> = {}): ScheduleDetail {
  const history = overrides.lifecycle_history ?? [lifecycle()];
  return {
    id: "sched-1",
    name: "nightly-build",
    description: null,
    enabled: 0,
    trigger_type: "cron",
    cron_expr: "0 * * * *",
    interval_sec: null,
    github_repo: null,
    poll_interval_sec: null,
    action_kind: "agent",
    action_model: null,
    action_prompt: "do the thing",
    action_agent: null,
    action_playbook: null,
    action_project: null,
    on_success: null,
    on_fail: null,
    last_fired_at: null,
    next_fire_at: null,
    missed_fire_policy: "skip",
    overlap_policy: "skip",
    project: null,
    created_at: 1_759_000_000_000,
    updated_at: 1_759_000_000_000,
    recent_runs: [],
    lifecycle_history: history,
    last_lifecycle_change: history[0] ?? null,
    ...overrides,
  };
}

let container: HTMLDivElement;
let root: Root;

async function mount() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <IntlProvider locale="en" messages={enMessages}>
        <ToastProvider>
          <ScheduleDetailModal scheduleId="sched-1" onClose={() => {}} onChanged={() => {}} />
        </ToastProvider>
      </IntlProvider>,
    );
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listScheduleRuns.mockResolvedValue({ runs: [] });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("lifecycle audit fields after a toggle", () => {
  it("re-reads the schedule so the panel does not keep its mount-time audit snapshot", async () => {
    const before = detail();
    const after = detail({
      enabled: 1,
      lifecycle_history: [
        lifecycle({
          id: "lc-2",
          status: "enabled",
          previous_status: "disabled",
          reason_summary: "resumed after the runner came back",
          created_at: 1_760_000_500_000,
        }),
        lifecycle(),
      ],
    });
    api.getSchedule.mockResolvedValueOnce(before).mockResolvedValueOnce(after);

    await mount();
    expect(container.textContent).toContain("paused while the runner was down");
    expect(container.textContent).not.toContain("resumed after the runner came back");

    const toggle = container.querySelector<HTMLButtonElement>('button[aria-pressed="false"]');
    expect(toggle, "the enable toggle should be rendered in the disabled state").not.toBeNull();
    await act(async () => {
      toggle!.click();
    });
    // Let the enable call, onToggled, and the refetch all settle.
    await act(async () => {
      await Promise.resolve();
    });

    expect(api.enableSchedule).toHaveBeenCalledWith("sched-1");
    expect(api.getSchedule).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain("resumed after the runner came back");
  });

  it("keeps the optimistic state when the re-read fails", async () => {
    api.getSchedule.mockResolvedValueOnce(detail()).mockRejectedValueOnce(new Error("offline"));

    await mount();
    const toggle = container.querySelector<HTMLButtonElement>('button[aria-pressed="false"]');
    await act(async () => {
      toggle!.click();
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(api.getSchedule).toHaveBeenCalledTimes(2);
    expect(
      container.querySelector<HTMLButtonElement>('button[aria-pressed="true"]'),
      "the toggle should still read as enabled after a failed re-read",
    ).not.toBeNull();
  });
});
