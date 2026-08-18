import { act, useEffect, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Modal from "./Modal";

/** Mirrors callers like CreateScheduleModal that focus their own first field
 *  in a mount effect, which runs before Modal's own mount effect. */
function SelfFocusingChild() {
  const wrapRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    wrapRef.current?.querySelector("input")?.focus();
  }, []);
  return (
    <form>
      <div ref={wrapRef}>
        <input aria-label="Schedule name" />
      </div>
    </form>
  );
}

describe("Modal keyboard and screen-reader behavior", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function renderModal(onClose = vi.fn()) {
    await act(async () => {
      root.render(
        <Modal title="Create schedule" closeLabel="Close" onClose={onClose}>
          <button type="button">First action</button>
          <button type="button">Last action</button>
        </Modal>,
      );
    });
    return onClose;
  }

  it("labels the dialog from its visible title and focuses inside it", async () => {
    await renderModal();
    const dialog = container.querySelector<HTMLElement>('[role="dialog"]');
    const title = container.querySelector("h2");

    expect(dialog?.getAttribute("aria-labelledby")).toBe(title?.id);
    expect(title?.textContent).toBe("Create schedule");
    expect(dialog?.contains(document.activeElement)).toBe(true);
  });

  it("wraps Tab and Shift+Tab within the dialog", async () => {
    await renderModal();
    const buttons = Array.from(container.querySelectorAll("button"));

    buttons.at(-1)?.focus();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    expect(document.activeElement).toBe(buttons[0]);

    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true }),
    );
    expect(document.activeElement).toBe(buttons.at(-1));
  });

  it("closes on Escape and restores focus when it unmounts", async () => {
    const launch = document.createElement("button");
    document.body.appendChild(launch);
    launch.focus();
    const onClose = await renderModal();

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(onClose).toHaveBeenCalledOnce();

    await act(async () => root.render(<div />));
    expect(document.activeElement).toBe(launch);
    launch.remove();
  });

  it("leaves focus with a child that focused its own field on mount", async () => {
    const launch = document.createElement("button");
    document.body.appendChild(launch);
    launch.focus();

    await act(async () => {
      root.render(
        <Modal title="New schedule" closeLabel="Close" onClose={vi.fn()}>
          <SelfFocusingChild />
        </Modal>,
      );
    });

    const active = document.activeElement as HTMLElement | null;
    expect(`${active?.tagName}:${active?.getAttribute("aria-label")}`).toBe("INPUT:Schedule name");

    await act(async () => root.render(<div />));
    expect(document.activeElement).toBe(launch);
    launch.remove();
  });

  it("does not reset focus when a caller supplies a new close callback", async () => {
    await renderModal();
    const lastAction = Array.from(container.querySelectorAll("button")).at(-1);
    lastAction?.focus();

    await act(async () => {
      root.render(
        <Modal title="Create schedule" closeLabel="Close" onClose={vi.fn()}>
          <button type="button">First action</button>
          <button type="button">Last action</button>
        </Modal>,
      );
    });

    expect(document.activeElement).toBe(lastAction);
  });

  it("pulls focus back into the dialog on Tab in either direction", async () => {
    const outside = document.createElement("button");
    document.body.appendChild(outside);
    await renderModal();
    const buttons = Array.from(container.querySelectorAll("button"));

    outside.focus();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    expect(document.activeElement).toBe(buttons[0]);

    outside.focus();
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true }),
    );
    expect(document.activeElement).toBe(buttons.at(-1));

    outside.remove();
  });
});
