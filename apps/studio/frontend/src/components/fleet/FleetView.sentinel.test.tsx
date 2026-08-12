/**
 * Fleet history — the load-more sentinel fires once per crossing.
 *
 * The load-more button doubles as the scroll sentinel, so an observer that is
 * torn down and re-created whenever the handler's identity changes will keep
 * re-firing: a newly observed target always receives an immediate initial
 * observation, and revealing rows is exactly what changes the handler's
 * identity. That feedback loop walks the whole run history in a single burst
 * rather than a page per scroll, which is both a load the server never asked
 * for and fast enough for React to abort the render as a runaway update.
 *
 * The fake below models the one behaviour that matters here: `observe()`
 * delivers an intersection immediately, the way a real IntersectionObserver
 * does for a freshly observed target.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { RecentRow } from "./fleetReducer";
import { HistorySection } from "./FleetView";

const ROW_COUNT = 40;
const START_VISIBLE = 2;
const STEP = 2;
// Cap the harness so a regression fails as a wrong count instead of hanging
// the suite. Well above the one call the fixed component makes, and above
// the ~20 the feedback loop needs to exhaust ROW_COUNT.
const RUNAWAY_CAP = 200;

function rows(n: number): RecentRow[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `run-${i}`,
    name: `run number ${i}`,
    status: "completed",
    invocation_id: null,
    endedAtSec: 1000 - i,
    totalCostUsd: null,
  }));
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  root = null;
  container = null;
  vi.unstubAllGlobals();
});

/** Renders HistorySection with a handler that reveals more rows — and, like
 *  the real `handleLoadMore`, gets a fresh identity every render. */
function Harness({ onCall }: { onCall: () => void }) {
  const [visibleCount, setVisibleCount] = React.useState(START_VISIBLE);
  const all = React.useMemo(() => rows(ROW_COUNT), []);
  const onLoadMore = () => {
    onCall();
    setVisibleCount((n) => (n > RUNAWAY_CAP ? n : n + STEP));
  };
  return (
    <HistorySection
      rows={all}
      filter="all"
      onFilter={() => {}}
      kind={null}
      onKind={() => {}}
      sort="recent"
      onSort={() => {}}
      selectedId={null}
      onSelect={() => {}}
      nowSec={2000}
      visibleCount={visibleCount}
      serverHasMore={false}
      loadingMore={false}
      onLoadMore={onLoadMore}
    />
  );
}

describe("fleet history — load-more sentinel", () => {
  it("reveals one page per intersection instead of walking the whole history", () => {
    class ImmediateObserver {
      private cb: (entries: { isIntersecting: boolean }[]) => void;
      constructor(cb: (entries: { isIntersecting: boolean }[]) => void) {
        this.cb = cb;
      }
      observe() {
        this.cb([{ isIntersecting: true }]);
      }
      disconnect() {}
      unobserve() {}
    }
    vi.stubGlobal("IntersectionObserver", ImmediateObserver);

    let calls = 0;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root!.render(
        <IntlProvider locale="en" messages={enMessages}>
          <Harness
            onCall={() => {
              calls += 1;
            }}
          />
        </IntlProvider>,
      );
    });

    // One crossing, one page. Re-arming on the handler's identity turned this
    // into a call per revealed page until the list ran out.
    expect(calls, "the sentinel re-armed itself and kept firing").toBe(1);

    // And the visible window grew by exactly one step, so rows past it are
    // still unrendered — the runaway revealed every row instead.
    const text = container.textContent ?? "";
    expect(text).toContain("run number 3");
    expect(text).not.toContain(`run number ${ROW_COUNT - 1}`);
  });
});
