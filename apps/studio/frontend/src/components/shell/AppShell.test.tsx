import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";

const operatorPanelRender = vi.hoisted(() => vi.fn());

vi.mock("@/components/operator/OperatorPanel", () => ({
  default: (props: { open: boolean; onClose: () => void }) => {
    operatorPanelRender(props);
    return <aside data-testid="operator-panel" />;
  },
}));

vi.mock("./IconRail", () => ({
  default: ({ onToggleOperator }: { onToggleOperator: () => void }) => (
    <button type="button" onClick={onToggleOperator}>
      Toggle Operator
    </button>
  ),
}));
vi.mock("./CommandPalette", () => ({ default: () => null }));
vi.mock("./StatusFooter", () => ({ default: () => null }));
vi.mock("./TopBar", () => ({ default: () => null }));

const { default: AppShell } = await import("./AppShell");

describe("AppShell — Operator lifecycle", () => {
  let container: HTMLDivElement;
  let root: Root | null;
  let originalInnerWidth: number;
  const storage = new Map<string, string>();

  beforeEach(() => {
    originalInnerWidth = window.innerWidth;
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    storage.clear();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => storage.set(key, value),
        removeItem: (key: string) => storage.delete(key),
        clear: () => storage.clear(),
      },
    });
    window.localStorage.setItem("studio:operator-visibility", "closed");
    operatorPanelRender.mockClear();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    if (root) act(() => root?.unmount());
    root = null;
    container.remove();
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: originalInnerWidth,
    });
    vi.unstubAllGlobals();
  });

  function renderShell() {
    act(() => {
      root?.render(
        <IntlProvider locale="en" messages={enMessages}>
          <AppShell onLocaleChange={vi.fn()}>
            <div>content</div>
          </AppShell>
        </IntlProvider>,
      );
    });
  }

  it("does not mount Operator while its persisted visibility is closed", () => {
    renderShell();

    expect(operatorPanelRender).not.toHaveBeenCalled();
    expect(container.querySelector('[data-testid="operator-panel"]')).toBeNull();
  });

  it("starts Operator closed on a fresh viewport too narrow for the 408px dock", () => {
    window.localStorage.clear();
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1_280 });

    renderShell();

    expect(operatorPanelRender).not.toHaveBeenCalled();
    expect(container.querySelector('[data-testid="operator-panel"]')).toBeNull();
  });

  it("mounts Operator on demand and removes it again when toggled closed", () => {
    renderShell();
    const toggle = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Toggle Operator",
    );

    act(() => toggle?.click());
    expect(operatorPanelRender).toHaveBeenCalledWith(expect.objectContaining({ open: true }));
    expect(container.querySelector('[data-testid="operator-panel"]')).not.toBeNull();

    act(() => toggle?.click());
    expect(container.querySelector('[data-testid="operator-panel"]')).toBeNull();
  });
});
