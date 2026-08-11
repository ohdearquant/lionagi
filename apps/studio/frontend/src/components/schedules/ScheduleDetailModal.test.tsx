import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "@/components/ui/Toast";
import enMessages from "@/messages/en.json";
import type { ScheduleDetail } from "@/lib/types";
import ScheduleDetailModal from "./ScheduleDetailModal";

const api = vi.hoisted(() => ({
  getSchedule: vi.fn(),
  listScheduleRuns: vi.fn(),
  getInvocation: vi.fn(),
  updateSchedule: vi.fn(),
  deleteSchedule: vi.fn(),
  triggerSchedule: vi.fn(),
  disableSchedule: vi.fn(),
  enableSchedule: vi.fn(),
}));
const router = vi.hoisted(() => ({
  navigate: vi.fn(),
  blocker: {
    status: "idle",
    current: undefined,
    next: undefined,
    action: undefined,
    proceed: undefined,
    reset: undefined,
  } as Record<string, unknown>,
  blockerOptions: undefined as
    | {
        shouldBlockFn: () => boolean;
        enableBeforeUnload: () => boolean;
      }
    | undefined,
}));

vi.mock("@/lib/api", () => api);
vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => router.navigate,
  useBlocker: (options: NonNullable<typeof router.blockerOptions>) => {
    router.blockerOptions = options;
    return router.blocker;
  },
}));

const detail: ScheduleDetail = {
  id: "schedule-1",
  name: "Nightly demo",
  description: "Build the demo",
  enabled: 1,
  trigger_type: "cron",
  cron_expr: "0 2 * * *",
  interval_sec: null,
  github_repo: null,
  poll_interval_sec: null,
  action_kind: "agent",
  action_model: null,
  action_prompt: "Prepare",
  action_agent: "demo-agent",
  action_playbook: null,
  action_project: null,
  on_success: null,
  on_fail: null,
  last_fired_at: null,
  next_fire_at: 1_700_000_000,
  missed_fire_policy: "skip",
  overlap_policy: "skip",
  project: null,
  created_at: 1_699_000_000,
  updated_at: 1_699_000_000,
  recent_runs: [],
};

describe("ScheduleDetailModal interactions", () => {
  let container: HTMLDivElement;
  let root: Root;
  let onClose: ReturnType<typeof vi.fn<() => void>>;

  async function renderModal() {
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <ToastProvider>
            <ScheduleDetailModal
              scheduleId="schedule-1"
              onClose={onClose}
              onChanged={vi.fn<() => void>()}
            />
          </ToastProvider>
        </IntlProvider>,
      );
      await Promise.resolve();
    });
  }

  function isReachableAtMobile(element: Element): boolean {
    let current: Element | null = element;
    while (current && current !== container.parentElement) {
      if (current.classList.contains("hidden")) return false;
      current = current.parentElement;
    }
    return true;
  }

  function mobileButtons(label: string): HTMLButtonElement[] {
    return Array.from(container.querySelectorAll<HTMLButtonElement>("button")).filter(
      (button) => button.textContent === label && isReachableAtMobile(button),
    );
  }

  function mobileSelect(label: string): HTMLSelectElement | undefined {
    return (
      Array.from(container.querySelectorAll<HTMLLabelElement>("label"))
        .filter((field) => isReachableAtMobile(field))
        .find((field) => field.querySelector(":scope > span")?.textContent === label)
        ?.querySelector<HTMLSelectElement>("select") ?? undefined
    );
  }

  beforeEach(async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("innerWidth", 390);
    vi.stubGlobal("innerHeight", 844);
    api.getSchedule.mockResolvedValue(detail);
    api.listScheduleRuns.mockResolvedValue({ runs: [], total: 0 });
    onClose = vi.fn<() => void>();
    router.navigate.mockReset();
    router.blocker = {
      status: "idle",
      current: undefined,
      next: undefined,
      action: undefined,
      proceed: undefined,
      reset: undefined,
    };
    router.blockerOptions = undefined;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await renderModal();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  function editName() {
    const input = container.querySelector<HTMLInputElement>('input[aria-label="Name"]');
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    act(() => {
      setValue?.call(input, "Changed demo");
      input?.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  it("exposes one mobile Delete control and one of each firing-policy selector in logical order", () => {
    const deleteButtons = mobileButtons("Delete");
    const missedFire = mobileSelect("Missed fire");
    const overlap = mobileSelect("Overlap");

    expect(deleteButtons).toHaveLength(1);
    expect(missedFire).toBeDefined();
    expect(overlap).toBeDefined();
    expect(deleteButtons[0].tabIndex).toBe(0);
    expect(missedFire?.tabIndex).toBe(0);
    expect(overlap?.tabIndex).toBe(0);
    expect(
      deleteButtons[0].compareDocumentPosition(missedFire!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      missedFire!.compareDocumentPosition(overlap!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it.each([
    ["Missed fire", "run_once"],
    ["Overlap", "queue"],
  ])("a mobile %s edit participates in the dirty-close guard", (label, value) => {
    const select = mobileSelect(label);
    expect(select).toBeDefined();
    const setValue = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    act(() => {
      setValue?.call(select, value);
      select?.dispatchEvent(new Event("change", { bubbles: true }));
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });

    expect(onClose).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "You have unsaved changes.",
    );
  });

  it("mobile Delete keeps its explicit two-step confirmation", async () => {
    const deleteButton = mobileButtons("Delete")[0];
    expect(deleteButton).toBeDefined();

    act(() => deleteButton?.click());
    expect(api.deleteSchedule).not.toHaveBeenCalled();
    expect(deleteButton?.textContent).toBe("Confirm delete");

    await act(async () => {
      deleteButton?.click();
      await Promise.resolve();
    });
    expect(api.deleteSchedule).toHaveBeenCalledWith(detail.id);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("asks before Cancel discards an edited schedule", () => {
    editName();
    const cancel = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Cancel",
    );
    act(() => cancel?.click());

    expect(onClose).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "You have unsaved changes.",
    );
    expect(document.activeElement?.textContent).toContain("Keep editing");

    const discard = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Discard changes",
    );
    act(() => discard?.click());
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("blocks route navigation and beforeunload while the schedule is dirty", async () => {
    editName();

    expect(router.blockerOptions?.shouldBlockFn()).toBe(true);
    expect(router.blockerOptions?.enableBeforeUnload()).toBe(true);

    const proceed = vi.fn();
    const reset = vi.fn();
    router.blocker = {
      status: "blocked",
      current: {},
      next: {},
      action: "BACK",
      proceed,
      reset,
    };
    await renderModal();

    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "You have unsaved changes.",
    );
    expect(document.activeElement?.textContent).toContain("Keep editing");

    const keepEditing = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Keep editing",
    );
    act(() => keepEditing?.click());
    expect(reset).toHaveBeenCalledOnce();
    expect(proceed).not.toHaveBeenCalled();
  });

  it("asks before Escape discards edits but closes an unchanged dialog", () => {
    editName();
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(onClose).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')).not.toBeNull();

    const keepEditing = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Keep editing",
    );
    act(() => keepEditing?.click());
    const input = container.querySelector<HTMLInputElement>('input[aria-label="Name"]');
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    act(() => {
      setValue?.call(input, detail.name);
      input?.dispatchEvent(new Event("input", { bubbles: true }));
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(onClose).toHaveBeenCalledOnce();
  });
});
