import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import enMessages from "@/messages/en.json";
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
});
