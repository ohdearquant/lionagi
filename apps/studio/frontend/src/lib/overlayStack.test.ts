import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  isTopmostOverlay,
  OverlayLayer,
  popOverlay,
  pushOverlay,
  SCROLL_LOCK_ATTRIBUTE,
} from "./overlayStack";

/** The stack is module state, so anything a test pushes it also releases. */
const opened: symbol[] = [];

function open(description: string, layer?: OverlayLayer): symbol {
  const token = layer === undefined ? pushOverlay(description) : pushOverlay(description, layer);
  opened.push(token);
  return token;
}

afterEach(() => {
  while (opened.length) popOverlay(opened.pop() as symbol);
});

describe("overlayStack", () => {
  it("gives the keyboard to the newest overlay on one layer", () => {
    const first = open("first");
    expect(isTopmostOverlay(first)).toBe(true);

    const second = open("second");
    expect(isTopmostOverlay(second)).toBe(true);
    expect(isTopmostOverlay(first)).toBe(false);
  });

  it("returns the keyboard when the overlay above it closes", () => {
    const below = open("below");
    const above = open("above");
    expect(isTopmostOverlay(below)).toBe(false);

    popOverlay(above);
    opened.pop();
    expect(isTopmostOverlay(below)).toBe(true);
  });

  it("keeps the keyboard on the shell layer when a routed overlay opens later", () => {
    // The case mount order gets wrong. AppShell renders the command palette
    // after the routed view, so the palette draws above a modal a route opens
    // while it is still up -- even though that modal registers second.
    const palette = open("CommandPalette", OverlayLayer.Shell);
    const routeModal = open("Modal");

    expect(isTopmostOverlay(palette)).toBe(true);
    expect(isTopmostOverlay(routeModal)).toBe(false);
  });

  it("gives the keyboard to the shell layer whichever order the two register", () => {
    const routeModal = open("Modal");
    const palette = open("CommandPalette", OverlayLayer.Shell);

    expect(isTopmostOverlay(palette)).toBe(true);
    expect(isTopmostOverlay(routeModal)).toBe(false);
  });

  it("hands a routed overlay the keyboard once the shell overlay closes", () => {
    const routeModal = open("Modal");
    const palette = open("CommandPalette", OverlayLayer.Shell);
    expect(isTopmostOverlay(routeModal)).toBe(false);

    popOverlay(palette);
    opened.splice(opened.indexOf(palette), 1);
    expect(isTopmostOverlay(routeModal)).toBe(true);
  });

  it("treats an overlay that never registered as not topmost", () => {
    open("registered");
    expect(isTopmostOverlay(Symbol("never registered"))).toBe(false);
  });

  it("leaves the stack alone when releasing a token twice", () => {
    const below = open("below");
    const above = open("above");

    popOverlay(above);
    opened.splice(opened.indexOf(above), 1);
    popOverlay(above);

    expect(isTopmostOverlay(below)).toBe(true);
  });

  it("has no owner at all when nothing is open", () => {
    expect(isTopmostOverlay(Symbol("anything"))).toBe(false);
  });

  describe("background isolation", () => {
    /**
     * The routed surface scrolls in its own container, so a lock on `body` alone
     * reads as held while the view still moves. Every arm below is written against
     * a marked container for that reason.
     */
    let scroller: HTMLElement;

    beforeEach(() => {
      scroller = document.createElement("div");
      scroller.setAttribute(SCROLL_LOCK_ATTRIBUTE, "");
      document.body.appendChild(scroller);
    });

    afterEach(() => {
      scroller.remove();
      document.body.style.overflow = "";
    });

    function release(token: symbol) {
      popOverlay(token);
      opened.splice(opened.indexOf(token), 1);
    }

    it("locks the marked scroll container while an overlay is open and releases it after the last one", () => {
      expect(scroller.style.overflow).toBe("");

      const token = open("only");
      expect(scroller.style.overflow).toBe("hidden");

      release(token);
      expect(scroller.style.overflow).toBe("");
    });

    it("locks body as well, for a route that scrolls there instead", () => {
      const token = open("only");
      expect(document.body.style.overflow).toBe("hidden");

      release(token);
      expect(document.body.style.overflow).toBe("");
    });

    it("keeps the container locked when a nested overlay closes above an open one", () => {
      const below = open("below");
      const above = open("above");

      release(above);
      expect(scroller.style.overflow).toBe("hidden");

      release(below);
      expect(scroller.style.overflow).toBe("");
    });

    it("restores each element's own overflow rather than clearing it", () => {
      scroller.style.overflow = "scroll";
      document.body.style.overflow = "auto";

      const token = open("only");
      expect(scroller.style.overflow).toBe("hidden");
      expect(document.body.style.overflow).toBe("hidden");

      release(token);
      expect(scroller.style.overflow).toBe("scroll");
      expect(document.body.style.overflow).toBe("auto");
    });
  });
});
