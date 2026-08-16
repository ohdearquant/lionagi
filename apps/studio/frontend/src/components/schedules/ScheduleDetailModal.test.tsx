import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Modal from "@/components/ui/Modal";
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

  function policySelect(label: string): HTMLSelectElement | null {
    return container.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`);
  }

  beforeEach(async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
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

  it.each([
    ["Missed fire", "run_once"],
    ["Overlap", "queue"],
  ])("a %s edit participates in the dirty-close guard", (label, value) => {
    const select = policySelect(label);
    expect(select).not.toBeNull();
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

  it("leaves the keyboard to a dialog opened over it", async () => {
    // This editor traps keys with a listener above the tree, and it was added
    // before the one on top, so it sees every key first. Without a check for
    // which surface is topmost it answers Escape meant for the newer dialog,
    // and its Tab trap pulls focus down out of it for the same reason.
    const closeOverlay = vi.fn<() => void>();
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <ToastProvider>
            <ScheduleDetailModal
              scheduleId="schedule-1"
              onClose={onClose}
              onChanged={vi.fn<() => void>()}
            />
            <Modal title="On top" closeLabel="Close overlay" onClose={closeOverlay}>
              <button type="button">Overlay action</button>
            </Modal>
          </ToastProvider>
        </IntlProvider>,
      );
      await Promise.resolve();
    });

    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });

    expect(closeOverlay).toHaveBeenCalledOnce();
    expect(onClose).not.toHaveBeenCalled();
  });
});
