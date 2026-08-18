import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import enMessages from "@/messages/en.json";
import Modal from "@/components/ui/Modal";
import CommandPalette from "./CommandPalette";

const commandAction = vi.fn();

vi.mock("@tanstack/react-router", () => ({ useNavigate: () => vi.fn() }));
vi.mock("@/lib/commands", () => ({
  fuzzyMatch: () => true,
  buildRegistry: () => [
    { id: "first", label: "First command", section: "Test", action: commandAction },
  ],
}));

describe("CommandPalette keyboard behavior", () => {
  let container: HTMLDivElement;
  let root: Root;
  let launcher: HTMLButtonElement;
  let onClose: ReturnType<typeof vi.fn<() => void>>;

  beforeEach(async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    commandAction.mockClear();
    onClose = vi.fn<() => void>();
    launcher = document.createElement("button");
    document.body.appendChild(launcher);
    launcher.focus();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <CommandPalette
            open
            onClose={onClose}
            toggleTheme={vi.fn<() => void>()}
            toggleOperator={vi.fn<() => void>()}
          />
        </IntlProvider>,
      );
    });
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    launcher.remove();
    vi.unstubAllGlobals();
  });

  it("does not execute the active command when Enter is pressed on Close", () => {
    const close = container.querySelector<HTMLButtonElement>('button[aria-label="Close"]');
    close?.focus();
    close?.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

    expect(commandAction).not.toHaveBeenCalled();
  });

  it("traps focus and restores the launch control after unmount", async () => {
    const input = container.querySelector<HTMLInputElement>('[role="combobox"]');
    const close = container.querySelector<HTMLButtonElement>('button[aria-label="Close"]');
    expect(document.activeElement).toBe(input);

    close?.focus();
    close?.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    expect(document.activeElement).toBe(input);

    input?.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true }),
    );
    expect(document.activeElement).toBe(close);

    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <CommandPalette
            open={false}
            onClose={onClose}
            toggleTheme={vi.fn<() => void>()}
            toggleOperator={vi.fn<() => void>()}
          />
        </IntlProvider>,
      );
    });
    expect(document.activeElement).toBe(launcher);
  });

  it("keeps focus inside itself when a dialog is already open underneath", async () => {
    // The dialog's trap was added first and reads the palette's focus as focus that escaped it.
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <Modal title="Underneath" closeLabel="Close dialog" onClose={vi.fn<() => void>()}>
            <button type="button">Dialog first</button>
            <button type="button">Dialog last</button>
          </Modal>
          <CommandPalette
            open
            onClose={onClose}
            toggleTheme={vi.fn<() => void>()}
            toggleOperator={vi.fn<() => void>()}
          />
        </IntlProvider>,
      );
    });

    const dialog = Array.from(container.querySelectorAll<HTMLElement>("button"))
      .find((button) => button.textContent === "Dialog first")
      ?.closest<HTMLElement>('[role="dialog"]');
    const input = container.querySelector<HTMLInputElement>('[role="combobox"]');
    const paletteClose = container.querySelector<HTMLButtonElement>('button[aria-label="Close"]');
    expect(dialog).toBeTruthy();
    expect(document.activeElement).toBe(input);

    input?.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true }),
    );

    expect(dialog?.contains(document.activeElement)).toBe(false);
    expect(document.activeElement).toBe(paletteClose);
  });

  it("leaves focus in an open palette when a route opens a dialog beneath it", async () => {
    // Registration order is not paint order; the keys stop React remounting the palette.
    const palette = (
      <CommandPalette
        key="palette"
        open
        onClose={onClose}
        toggleTheme={vi.fn<() => void>()}
        toggleOperator={vi.fn<() => void>()}
      />
    );
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          {palette}
        </IntlProvider>,
      );
    });
    const input = container.querySelector<HTMLInputElement>('[role="combobox"]');
    expect(document.activeElement).toBe(input);

    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <Modal
            key="routed"
            title="Underneath"
            closeLabel="Close dialog"
            onClose={vi.fn<() => void>()}
          >
            <button type="button">Dialog first</button>
          </Modal>
          {palette}
        </IntlProvider>,
      );
    });

    const dialog = Array.from(container.querySelectorAll<HTMLElement>("button"))
      .find((button) => button.textContent === "Dialog first")
      ?.closest<HTMLElement>('[role="dialog"]');
    // Premises: the dialog mounted and the palette was not remounted.
    expect(dialog).toBeTruthy();
    expect(container.querySelector('[role="combobox"]')).toBe(input);
    expect(dialog?.contains(document.activeElement)).toBe(false);
    expect(document.activeElement).toBe(input);

    // Closing must not restore focus it never held.
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          {palette}
        </IntlProvider>,
      );
    });
    expect(document.activeElement).toBe(input);
  });

  it("closes only the topmost surface on Escape", async () => {
    const closeDialog = vi.fn<() => void>();
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <Modal title="Underneath" closeLabel="Close dialog" onClose={closeDialog}>
            <button type="button">Dialog first</button>
          </Modal>
          <CommandPalette
            open
            onClose={onClose}
            toggleTheme={vi.fn<() => void>()}
            toggleOperator={vi.fn<() => void>()}
          />
        </IntlProvider>,
      );
    });

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

    expect(onClose).toHaveBeenCalledOnce();
    expect(closeDialog).not.toHaveBeenCalled();
  });

  it("hands focus to the dialog beneath once the palette closes", async () => {
    const dialog = (
      <Modal
        key="routed"
        title="Underneath"
        closeLabel="Close dialog"
        onClose={vi.fn<() => void>()}
      >
        <button type="button">Dialog first</button>
      </Modal>
    );
    const palette = (
      <CommandPalette
        key="palette"
        open
        onClose={onClose}
        toggleTheme={vi.fn<() => void>()}
        toggleOperator={vi.fn<() => void>()}
      />
    );

    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          {palette}
        </IntlProvider>,
      );
    });
    const input = container.querySelector<HTMLInputElement>('[role="combobox"]');
    expect(document.activeElement).toBe(input);

    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          {dialog}
          {palette}
        </IntlProvider>,
      );
    });
    const dialogEl = Array.from(container.querySelectorAll<HTMLElement>("button"))
      .find((button) => button.textContent === "Dialog first")
      ?.closest<HTMLElement>('[role="dialog"]');
    // Premises: it mounted beneath, and declined the claim while the palette held it.
    expect(dialogEl).toBeTruthy();
    expect(document.activeElement).toBe(input);

    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          {dialog}
        </IntlProvider>,
      );
    });
    expect(container.querySelector('[role="combobox"]')).toBeNull();
    expect(dialogEl?.contains(document.activeElement)).toBe(true);
  });
});
