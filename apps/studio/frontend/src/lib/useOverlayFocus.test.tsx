import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useOverlayFocus } from "./useOverlayFocus";

function Dialog({
  label,
  onEscape = () => {},
  children,
}: {
  label: string;
  onEscape?: () => void;
  children?: React.ReactNode;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useOverlayFocus({ description: label, dialogRef, onEscape });
  return (
    <div ref={dialogRef} role="dialog" aria-label={label} tabIndex={-1}>
      {children}
    </div>
  );
}

describe("useOverlayFocus", () => {
  let container: HTMLDivElement;
  let root: Root;
  let launcher: HTMLButtonElement;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    container = document.createElement("div");
    document.body.appendChild(container);
    launcher = document.createElement("button");
    document.body.appendChild(launcher);
    launcher.focus();
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    launcher.remove();
    vi.unstubAllGlobals();
  });

  function dialogEl(label: string) {
    return container.querySelector<HTMLElement>(`[aria-label="${label}"]`);
  }

  it("keeps Tab inside a dialog that has nothing focusable in it", async () => {
    await act(async () => root.render(<Dialog label="empty" />));
    const dialog = dialogEl("empty");
    // Premise: the guard is only meaningful when the list really is empty.
    expect(dialog?.querySelectorAll("button, input, [href]").length).toBe(0);
    expect(document.activeElement).toBe(dialog);

    // jsdom never moves focus on Tab, so only the cancellation is observable here:
    // unprevented, a real browser walks the caret out of an aria-modal dialog.
    const tab = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    act(() => {
      document.dispatchEvent(tab);
    });
    expect(tab.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(dialog);
  });

  it("restores the launcher on close, which requires reading ownership before the pop", async () => {
    await act(async () =>
      root.render(
        <Dialog label="solo">
          <button type="button">Inside</button>
        </Dialog>,
      ),
    );
    expect(dialogEl("solo")?.contains(document.activeElement)).toBe(true);

    await act(async () => root.render(<div />));
    // Reading ownership after popOverlay would make this restore never fire at all.
    expect(document.activeElement).toBe(launcher);
  });

  it("does not restore when something is painted above it", async () => {
    await act(async () =>
      root.render(
        <>
          <Dialog label="under">
            <button type="button">Under action</button>
          </Dialog>
          <Dialog label="over">
            <button type="button">Over action</button>
          </Dialog>
        </>,
      ),
    );
    expect(dialogEl("over")?.contains(document.activeElement)).toBe(true);

    await act(async () =>
      root.render(
        <>
          {null}
          <Dialog label="over">
            <button type="button">Over action</button>
          </Dialog>
        </>,
      ),
    );
    expect(document.activeElement).not.toBe(launcher);
    expect(dialogEl("over")?.contains(document.activeElement)).toBe(true);
  });

  it("leaves the caret untouched in the overlay above when it closes underneath one", async () => {
    const over = (
      <Dialog label="over">
        <button type="button">First</button>
        <button type="button">Second</button>
      </Dialog>
    );
    await act(async () =>
      root.render(
        <>
          <Dialog label="under">
            <button type="button">Under action</button>
          </Dialog>
          {over}
        </>,
      ),
    );
    const second = dialogEl("over")?.querySelectorAll("button")[1] as HTMLButtonElement;
    act(() => second.focus());

    await act(async () =>
      root.render(
        <>
          {null}
          {over}
        </>,
      ),
    );
    // A restore that ignores ownership lands on the launcher, and the reclaim that
    // follows the pop puts the caret on the FIRST child, not back where it was.
    expect(second.isConnected).toBe(true);
    expect(document.activeElement).toBe(second);
  });

  it("claims the keyboard when the overlay above it closes", async () => {
    await act(async () =>
      root.render(
        <>
          <Dialog label="under">
            <button type="button">Under action</button>
          </Dialog>
          <Dialog label="over">
            <button type="button">Over action</button>
          </Dialog>
        </>,
      ),
    );
    expect(dialogEl("under")?.contains(document.activeElement)).toBe(false);

    await act(async () =>
      root.render(
        <>
          <Dialog label="under">
            <button type="button">Under action</button>
          </Dialog>
          {null}
        </>,
      ),
    );
    expect(dialogEl("under")?.contains(document.activeElement)).toBe(true);
  });

  it("routes Escape to the topmost overlay only", async () => {
    const under = vi.fn<() => void>();
    const over = vi.fn<() => void>();
    await act(async () =>
      root.render(
        <>
          <Dialog label="under" onEscape={under}>
            <button type="button">Under action</button>
          </Dialog>
          <Dialog label="over" onEscape={over}>
            <button type="button">Over action</button>
          </Dialog>
        </>,
      ),
    );

    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(over).toHaveBeenCalledOnce();
    expect(under).not.toHaveBeenCalled();
  });
});
