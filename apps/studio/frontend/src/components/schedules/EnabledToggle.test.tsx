import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "@/components/ui/Toast";
import enMessages from "@/messages/en.json";
import EnabledToggle from "./EnabledToggle";

const api = vi.hoisted(() => ({
  disableSchedule: vi.fn(),
  enableSchedule: vi.fn(),
}));

vi.mock("@/lib/api", () => api);

function setTextAreaValue(element: HTMLTextAreaElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(
    element,
    value,
  );
  element.dispatchEvent(new Event("input", { bubbles: true }));
}

function buttonNamed(root: ParentNode, name: RegExp): HTMLButtonElement | undefined {
  return Array.from(root.querySelectorAll<HTMLButtonElement>("button")).find((button) =>
    name.test(button.getAttribute("aria-label") ?? button.textContent ?? ""),
  );
}

describe("EnabledToggle disable reason", () => {
  let container: HTMLDivElement;
  let root: Root;
  let onToggled: ReturnType<typeof vi.fn<() => void>>;

  beforeEach(() => {
    api.disableSchedule.mockReset().mockResolvedValue({ ok: true, enabled: false });
    api.enableSchedule.mockReset().mockResolvedValue({ ok: true, enabled: true });
    onToggled = vi.fn<() => void>();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  async function mount(enabled: boolean) {
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <ToastProvider>
            <EnabledToggle scheduleId="schedule-1" enabled={enabled} onToggled={onToggled} />
          </ToastProvider>
        </IntlProvider>,
      );
    });
  }

  async function openDisableDialog(): Promise<HTMLElement> {
    const toggle = buttonNamed(container, /^Disable schedule$/i);
    expect(toggle).toBeDefined();
    await act(async () => toggle?.click());

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]');
    expect(dialog).not.toBeNull();
    return dialog!;
  }

  it("requires a reason before disabling an enabled schedule", async () => {
    await mount(true);
    const dialog = await openDisableDialog();
    const reason = dialog.querySelector<HTMLTextAreaElement>('textarea[aria-label="Reason"]');
    const submit = buttonNamed(dialog, /^Disable schedule$/i);

    expect(reason).not.toBeNull();
    expect(submit?.disabled).toBe(true);
    await act(async () => setTextAreaValue(reason!, "   "));
    expect(submit?.disabled).toBe(true);
    expect(api.disableSchedule).not.toHaveBeenCalled();
  });

  it("submits the reason and reports the completed toggle", async () => {
    await mount(true);
    const dialog = await openDisableDialog();
    const reason = dialog.querySelector<HTMLTextAreaElement>('textarea[aria-label="Reason"]');

    await act(async () => setTextAreaValue(reason!, "Maintenance window"));
    const submit = buttonNamed(dialog, /^Disable schedule$/i);
    expect(submit?.disabled).toBe(false);
    await act(async () => submit?.click());

    expect(api.disableSchedule).toHaveBeenCalledOnce();
    expect(api.disableSchedule).toHaveBeenCalledWith("schedule-1", {
      reason: "Maintenance window",
    });
    expect(onToggled).toHaveBeenCalledOnce();
  });

  it("keeps the dialog and reason when disabling fails", async () => {
    api.disableSchedule.mockRejectedValueOnce(new Error("request failed"));
    await mount(true);
    const dialog = await openDisableDialog();
    const reason = dialog.querySelector<HTMLTextAreaElement>('textarea[aria-label="Reason"]');

    await act(async () => setTextAreaValue(reason!, "Investigating failures"));
    await act(async () => buttonNamed(dialog, /^Disable schedule$/i)?.click());

    const retainedDialog = document.body.querySelector<HTMLElement>('[role="dialog"]');
    const retainedReason = retainedDialog?.querySelector<HTMLTextAreaElement>(
      'textarea[aria-label="Reason"]',
    );
    expect(retainedDialog).not.toBeNull();
    expect(retainedReason?.value).toBe("Investigating failures");
    expect(onToggled).not.toHaveBeenCalled();
  });

  it("enables a disabled schedule without opening the reason dialog", async () => {
    await mount(false);
    const toggle = buttonNamed(container, /^Enable schedule$/i);

    expect(toggle).toBeDefined();
    await act(async () => toggle?.click());

    expect(api.enableSchedule).toHaveBeenCalledOnce();
    expect(api.enableSchedule).toHaveBeenCalledWith("schedule-1");
    expect(api.disableSchedule).not.toHaveBeenCalled();
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
    expect(onToggled).toHaveBeenCalledOnce();
  });
});
