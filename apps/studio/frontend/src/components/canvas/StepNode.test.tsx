/**
 * The card is read at a glance, in bulk, at whatever zoom fits the graph. That
 * only works if the same fact is always in the same corner, so these tests are
 * about the card keeping its shape rather than about any one string.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import StepNode, { MAX_ANIMATING_NODES } from "./StepNode";
import type { StepNodeData, NodeExecStatus } from "./StepNode";
import { NODE_HEIGHT } from "./useLayout";
import { STALL_TIMEOUT_MS } from "@/lib/nodeActivity";

// Handle needs a ReactFlow store; the card's own layout is what is under test.
vi.mock("reactflow", () => ({
  Handle: () => null,
  Position: { Left: "left", Right: "right" },
}));

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  // The card asks for the reduced-motion preference on mount; this environment
  // has no matchMedia. Answering "no preference" keeps the running animation on,
  // which is the arm these tests render under.
  vi.stubGlobal("matchMedia", () => ({
    matches: false,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

function baseData(data: Partial<StepNodeData>): StepNodeData {
  return {
    label: "plan-step",
    role: "critic",
    assignment: "",
    prompt: "",
    capacity: 1,
    timeout: null,
    inputs: [],
    outputs: [],
    ...data,
  };
}

function renderNode(data: Partial<StepNodeData>, id?: string) {
  const full = baseData(data);
  act(() => {
    // NodeProps carries more than the card reads; the rest is ReactFlow's.
    root.render(
      <IntlProvider locale="en" messages={enMessages}>
        {React.createElement(StepNode, { data: full, selected: false, id } as never)}
      </IntlProvider>,
    );
  });
}

/** Renders several cards at once, sharing one animation-slot registry pass —
 *  needed for the concurrent-animation-cap tests. */
function renderNodes(entries: Array<{ id: string; data: Partial<StepNodeData> }>) {
  act(() => {
    root.render(
      <IntlProvider locale="en" messages={enMessages}>
        {entries.map(({ id, data }) =>
          React.createElement(StepNode, {
            key: id,
            id,
            data: baseData(data),
            selected: false,
          } as never),
        )}
      </IntlProvider>,
    );
  });
}

/** The card's content rows, in order: name/state, role/elapsed, live-activity. */
function rows(): Element[] {
  return Array.from(container.querySelectorAll(":scope > div > div"));
}

function bottomRightText(): string {
  const bottom = rows()[1];
  const spans = bottom.querySelectorAll("span");
  return spans[spans.length - 1]?.textContent ?? "";
}

/** The live-activity row's header line (activity word + counter). */
function activityText(): string {
  return rows()[2]?.textContent ?? "";
}

function cards(): Element[] {
  return Array.from(container.children);
}

function isAnimating(card: Element): boolean {
  const dot = card.querySelector("span.animate-pulse");
  return dot !== null;
}

describe("StepNode — the bottom-right corner always says something", () => {
  it("shows elapsed time once there is any", () => {
    renderNode({ durationSeconds: 84, execStatus: "completed" });
    expect(bottomRightText()).toBe("1m");
  });

  it("shows the status word before there is a duration, rather than nothing", () => {
    // A corner that can go empty makes the card change shape mid-run, which is
    // exactly when a reader is scanning it.
    for (const [status, word] of [
      ["queued", "queued"],
      ["running", "running"],
      ["failed", "failed"],
      ["awaiting_approval", "approval"],
    ] as [NodeExecStatus, string][]) {
      renderNode({ execStatus: status });
      expect(bottomRightText()).toBe(word);
    }
  });

  it("prefers the duration over the status word when both could apply", () => {
    renderNode({ execStatus: "running", durationSeconds: 3.5 });
    expect(bottomRightText()).toBe("3.5s");
  });

  it("treats a negative duration as no duration, not as a printable number", () => {
    renderNode({ execStatus: "queued", durationSeconds: -1 });
    expect(bottomRightText()).toBe("queued");
  });

  it("is never blank in any status the card can be in", () => {
    const every: NodeExecStatus[] = [
      "pending",
      "queued",
      "running",
      "awaiting_approval",
      "paused",
      "completed",
      "failed",
      "escalated",
    ];
    for (const status of every) {
      renderNode({ execStatus: status });
      expect(bottomRightText().trim().length).toBeGreaterThan(0);
    }
  });
});

describe("StepNode — the card keeps its shape", () => {
  it("renders all three rows even when the node carries no role", () => {
    // Held open rather than dropped: a missing role must not move the row above
    // it, because one height is what the layout reserves for every node.
    renderNode({ role: "" });
    expect(rows().length).toBe(3);
  });

  it("renders all three rows for a node carrying nothing but a label", () => {
    renderNode({ role: "", execStatus: undefined, durationSeconds: undefined });
    expect(rows().length).toBe(3);
    expect(bottomRightText().trim().length).toBeGreaterThan(0);
  });

  it("puts the error count in the top row beside the state, not in the magnitude corner", () => {
    renderNode({ execStatus: "failed", errorCount: 3, durationSeconds: 12 });
    expect(rows()[0].textContent).toContain("3");
    expect(bottomRightText()).toBe("12s");
  });

  it("gives escalated a warning icon while failed keeps the error icon", () => {
    renderNode({ execStatus: "escalated" });
    const escalatedIcon = rows()[0].querySelector("span.flex.shrink-0.items-center");
    expect(escalatedIcon?.className).toContain("text-status-warning");
    expect(escalatedIcon?.className).not.toContain("text-status-error");

    renderNode({ execStatus: "failed" });
    const failedIcon = rows()[0].querySelector("span.flex.shrink-0.items-center");
    expect(failedIcon?.className).toContain("text-status-error");
    expect(failedIcon?.className).not.toContain("text-status-warning");
  });
});

describe("StepNode — one height for every state of a run", () => {
  it("is the same height for two side-by-side nodes whether or not either has finished", () => {
    // Compared against each other rather than against NODE_HEIGHT: the card
    // sets its height FROM that constant, so asserting it equals the constant
    // holds for any value and says nothing.
    renderNodes([
      { id: "unfinished", data: { execStatus: "running", activity: "thinking" } },
      { id: "finished", data: { execStatus: "completed" } },
    ]);
    const heights = cards().map((c) => (c as HTMLElement).style.height);
    expect(new Set(heights).size).toBe(1);
  });

  it("holds its height when a running node has an activity to report", () => {
    renderNodes([
      { id: "quiet", data: { execStatus: "running" } },
      { id: "busy", data: { execStatus: "running", activity: "tool", activityDetail: "grep" } },
    ]);
    const heights = cards().map((c) => (c as HTMLElement).style.height);
    expect(new Set(heights).size).toBe(1);
  });
});

describe("StepNode — a stall timeout returns the node to a static 'stalled' state", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("pulses right up to the stall timeout, then goes static and says it stalled", () => {
    const now = Date.now();
    renderNode({ execStatus: "running", lastEventAt: now, activity: "thinking" });
    const card = container.firstElementChild as HTMLElement;
    expect(isAnimating(card)).toBe(true);
    expect(activityText()).not.toContain("stalled");

    act(() => {
      vi.advanceTimersByTime(STALL_TIMEOUT_MS + 1);
    });

    expect(isAnimating(card)).toBe(false);
    expect(activityText()).toContain("stalled");
  });

  it("never stalls a node still receiving fresh events at the same cadence", () => {
    const now = Date.now();
    renderNode({ execStatus: "running", lastEventAt: now, activity: "thinking" });

    act(() => {
      vi.advanceTimersByTime(STALL_TIMEOUT_MS - 1);
    });
    // A fresh event lands just under the deadline, resetting the window.
    renderNode({ execStatus: "running", lastEventAt: Date.now(), activity: "thinking" });
    act(() => {
      vi.advanceTimersByTime(STALL_TIMEOUT_MS - 1);
    });

    const card = container.firstElementChild as HTMLElement;
    expect(isAnimating(card)).toBe(true);
    expect(activityText()).not.toContain("stalled");
  });
});

describe("StepNode — prefers-reduced-motion carries the same information, not less", () => {
  it("shows the identical activity word and counter whether or not motion is reduced", () => {
    const props: Partial<StepNodeData> = {
      execStatus: "running",
      lastEventAt: Date.now(),
      activity: "tool",
      activityDetail: "run_tests",
      counter: 128,
    };

    renderNode(props);
    const animatedText = activityText();
    const animatedHasPulse = isAnimating(container.firstElementChild as HTMLElement);

    // Force a real remount so the reduced-motion hook re-reads matchMedia —
    // it only asks on mount, same as the running card does today.
    act(() => root.unmount());
    root = createRoot(container);
    vi.stubGlobal("matchMedia", () => ({
      matches: true,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    renderNode(props);
    const reducedText = activityText();
    const reducedHasPulse = isAnimating(container.firstElementChild as HTMLElement);

    expect(animatedHasPulse).toBe(true);
    expect(reducedHasPulse).toBe(false);
    // The information — not merely "did it animate" — must match exactly.
    expect(reducedText).toBe(animatedText);
    expect(reducedText).toContain("128");
    expect(reducedText).toContain("run_tests");
  });
});

describe("StepNode — an absent activity signal is not a claim that the node is waiting", () => {
  it("gives a running node with no activity signal its status word, not 'waiting'", () => {
    // This is what the canvas renders for every running node today, because
    // nothing supplies the activity fields yet. Saying "waiting" next to a
    // node that is visibly running states something false about the run;
    // absence of a signal is not evidence of waiting.
    renderNode({ execStatus: "running", lastEventAt: Date.now() });

    // Assert the word that must be there, not just the absence of the wrong
    // one: an empty activity row would satisfy a "does not say waiting" check
    // while dropping the caption entirely and silently shortening the row.
    expect(activityText()).toContain("running");
    expect(activityText()).not.toContain("waiting");
  });

  it("still calls a queued node waiting, because a queued node is waiting", () => {
    // Guards the other direction, so the fix above cannot be satisfied by
    // removing the word everywhere: "waiting" is correct for a queued node and
    // has to survive.
    renderNode({ execStatus: "queued" });

    expect(activityText()).toContain("waiting");
  });
});

describe("StepNode — the concurrent-animation cap keeps a big canvas responsive", () => {
  it("animates at most MAX_ANIMATING_NODES nodes when more than that many are running", () => {
    const total = MAX_ANIMATING_NODES + 3;
    const now = Date.now();
    const entries = Array.from({ length: total }, (_, i) => ({
      id: `n${i}`,
      data: { execStatus: "running" as const, lastEventAt: now, activity: "thinking" as const },
    }));
    renderNodes(entries);

    expect(cards().length).toBe(total);
    const animatingCount = cards().filter(isAnimating).length;
    expect(animatingCount).toBe(MAX_ANIMATING_NODES);
  });
});

/**
 * Two canvases over the same run, mounted at once. The run detail keeps its
 * inline canvas mounted while the expanded graph is open, so both sets of cards
 * are live and carry identical node IDs. Every test above renders one canvas,
 * and one canvas cannot exhibit this at all.
 */
function renderTwoCanvases(
  inline: Array<{ id: string; data: Partial<StepNodeData> }>,
  expanded: Array<{ id: string; data: Partial<StepNodeData> }>,
) {
  const groups: Array<[string, Array<{ id: string; data: Partial<StepNodeData> }>]> = [
    ["inline", inline],
    ["expanded", expanded],
  ];
  act(() => {
    root.render(
      <IntlProvider locale="en" messages={enMessages}>
        {groups.flatMap(([canvas, entries]) =>
          entries.map(({ id, data }) =>
            React.createElement(StepNode, {
              // The React key separates the two cards; the node `id` they are
              // handed is deliberately the same, which is the real situation.
              key: `${canvas}-${id}`,
              id,
              data: baseData(data),
              selected: false,
            } as never),
          ),
        )}
      </IntlProvider>,
    );
  });
}

function runningNodes(count: number, prefix = "n") {
  const now = Date.now();
  return Array.from({ length: count }, (_, i) => ({
    id: `${prefix}${i}`,
    data: { execStatus: "running" as const, lastEventAt: now, activity: "thinking" as const },
  }));
}

describe("StepNode — the animation budget survives two canvases over one run", () => {
  it("still animates at most MAX_ANIMATING_NODES when the expanded graph doubles the cards", () => {
    // Both canvases show the same MAX_ANIMATING_NODES running nodes, so twice
    // the cap is on screen carrying only `cap` distinct IDs. Keyed by node ID,
    // the second canvas's cards each find their ID already registered and
    // animate without taking a slot: every card moves, the registry still reads
    // `cap`, and nothing anywhere reports that the budget was exceeded.
    const nodes = runningNodes(MAX_ANIMATING_NODES);
    renderTwoCanvases(nodes, nodes);

    expect(cards().length).toBe(MAX_ANIMATING_NODES * 2);
    expect(cards().filter(isAnimating).length).toBe(MAX_ANIMATING_NODES);
  });

  it("does not free a slot the surviving canvas is still animating on", () => {
    // Collapsing the expanded graph unmounts one card per node while the inline
    // card keeps animating. Keyed by node ID, that unmount deletes the entry the
    // inline card is still using, emptying the registry while `cap` nodes are
    // visibly moving — so the next node to start running claims a slot that is
    // already spoken for, and the canvas ends up over budget.
    const shared = runningNodes(MAX_ANIMATING_NODES);
    renderTwoCanvases(shared, shared);

    // Expanded canvas closes; a new node starts running in the same commit.
    renderTwoCanvases(shared, runningNodes(1, "late"));

    expect(cards().length).toBe(MAX_ANIMATING_NODES + 1);
    expect(cards().filter(isAnimating).length).toBe(MAX_ANIMATING_NODES);
  });

  it("releases a slot when a holder stops, so a newly mounted card can take it", () => {
    // The guard against satisfying the two above by never releasing anything.
    // It arrives as a fresh card rather than one already turned away, because a
    // denied card does not re-compete: slots are first-claimed-first-served and
    // a card that lost re-runs its effect only if its own inputs change. That
    // is the existing behaviour, unchanged here — the excess falls back to the
    // static state, which is what the cap is for.
    const shared = runningNodes(MAX_ANIMATING_NODES);
    renderTwoCanvases(shared, []);
    expect(cards().filter(isAnimating).length).toBe(MAX_ANIMATING_NODES);

    // One holder completes and a new node starts running in the same commit.
    const [first, ...rest] = shared;
    renderTwoCanvases(
      [{ id: first.id, data: { execStatus: "completed" as const } }, ...rest],
      runningNodes(1, "late"),
    );

    expect(cards().filter(isAnimating).length).toBe(MAX_ANIMATING_NODES);
    expect(isAnimating(cards()[0])).toBe(false);
    expect(isAnimating(cards()[cards().length - 1])).toBe(true);
  });
});

describe("StepNode — nothing animates outside the viewport", () => {
  it("does not animate a running node the IntersectionObserver reports as not intersecting", () => {
    class NotIntersectingObserver {
      constructor(private cb: IntersectionObserverCallback) {}
      observe(target: Element) {
        this.cb([{ isIntersecting: false, target } as IntersectionObserverEntry], this as never);
      }
      disconnect() {}
      unobserve() {}
    }
    vi.stubGlobal("IntersectionObserver", NotIntersectingObserver);

    renderNode({ execStatus: "running", lastEventAt: Date.now(), activity: "thinking" });
    const card = container.firstElementChild as HTMLElement;
    expect(isAnimating(card)).toBe(false);
  });

  it("animates a running node the IntersectionObserver reports as intersecting", () => {
    class IntersectingObserver {
      constructor(private cb: IntersectionObserverCallback) {}
      observe(target: Element) {
        this.cb([{ isIntersecting: true, target } as IntersectionObserverEntry], this as never);
      }
      disconnect() {}
      unobserve() {}
    }
    vi.stubGlobal("IntersectionObserver", IntersectingObserver);

    renderNode({ execStatus: "running", lastEventAt: Date.now(), activity: "thinking" });
    const card = container.firstElementChild as HTMLElement;
    expect(isAnimating(card)).toBe(true);
  });
});

describe("StepNode — the card reserves exactly the rows it draws", () => {
  it("reserves the chrome plus every row, and nothing for a row it does not draw", () => {
    // NODE_HEIGHT is a sum of its terms rather than a literal, because a
    // literal can only be checked against itself: the card sets its height
    // from the constant, so a test comparing the two passes at any value —
    // including 88, which it was while the card drew 98 worth of rows.
    //
    // Re-derived here from the type scale, so removing a row without
    // shrinking the reservation (or adding one without growing it) fails.
    const chrome = 8 * 2 + 3 * 2; // py-2 both sides, the running border both sides
    const nameRow = Math.ceil(12 * 1.375);
    const roleRow = Math.ceil(11 * 1.25);
    const activityRow = Math.ceil(11 * 1.25);
    const gapAboveActivity = 2;
    expect(NODE_HEIGHT).toBe(chrome + nameRow + roleRow + gapAboveActivity + activityRow);
  });

  it("draws no reserved-height block with nothing in it", () => {
    // The card used to keep two lines for the agent's latest text. No signal
    // carries per-node text, so that block rendered empty on every card of
    // every run. This fails if a reservation returns before a producer does.
    renderNode({ execStatus: "running", activity: "thinking" });
    const card = container.firstElementChild as HTMLElement;
    const emptyReservations = Array.from(card.querySelectorAll<HTMLElement>("*")).filter(
      (el) => el.style.height !== "" && el.textContent === "",
    );
    expect(emptyReservations).toEqual([]);
  });
});
