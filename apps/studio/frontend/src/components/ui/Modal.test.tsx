import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Modal from "./Modal";

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
});
