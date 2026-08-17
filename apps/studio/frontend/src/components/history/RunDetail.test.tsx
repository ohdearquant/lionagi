/**
 * RunDetail contract tests.
 *
 * Verifies:
 * - RunDetail.tsx exists and exports a default component
 * - It does not import Drawer (master-detail doctrine)
 */

import { afterEach, beforeAll, beforeEach, describe, it, expect, vi } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import RunStepCard from "@/components/RunStepCard";
import enMessages from "@/messages/en.json";
import type { RunStep, WorkerGraph } from "@/lib/types";

vi.mock("@/components/ui/Markdown", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

// Mounting RunDetail for real exercises the hidden-count badge + toggle as an
// actual render/click, not a source-text regex (which can pass while JSX
// placement or the click handler is broken). Everything mounted needs real
// network/router-context dependencies stubbed: getSession/streamSession/
// streamSignals hit real SSE/fetch plumbing, ResumeRun renders a
// @tanstack/react-router <Link> that throws outside a RouterProvider, and the
// real WorkerCanvas drags in dagre + the full ReactFlow tree, none of which
// this test needs — only that it received the right edge set.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getSession: vi.fn(),
    getInvocation: vi.fn(),
    streamSession: vi.fn(() => () => {}),
    streamSignals: vi.fn(() => () => {}),
  };
});

vi.mock("@/components/history/ResumeRun", () => ({
  default: () => null,
}));

vi.mock("@/components/canvas/WorkerCanvas", () => ({
  default: (props: { graph: { edges: unknown[] } }) => (
    <div data-testid="worker-canvas" data-edge-count={props.graph.edges.length} />
  ),
}));

// The control section renders only while some verb has a backing command.
// Every wrapper below delegates to the real implementation, so a test says
// nothing by accident; the control tests opt in to a backed registry, because
// the shown-and-disabled contract they assert is what exists once a command
// type lands, not what exists today. The run-swap test additionally overrides
// applyExecutablePath and the two dispatch calls, because the state it is
// about — a pause the user actually accepted — cannot be reached at all while
// every verb is refused before it is offered.
vi.mock("@/lib/runControls", async () => {
  const actual = await vi.importActual<typeof import("@/lib/runControls")>("@/lib/runControls");
  return {
    ...actual,
    hasAnyExecutablePath: vi.fn(actual.hasAnyExecutablePath),
    applyExecutablePath: vi.fn(actual.applyExecutablePath),
    proposeRunControl: vi.fn(actual.proposeRunControl),
    confirmRunControl: vi.fn(actual.confirmRunControl),
  };
});

const HISTORY_DIR = path.resolve(__dirname);
const mountedCards: Array<{ container: HTMLDivElement; root: Root }> = [];

afterEach(() => {
  for (const { container, root } of mountedCards) {
    act(() => root.unmount());
    container.remove();
  }
  mountedCards.length = 0;
});

function renderRunStepCards(steps: RunStep[], defaultExpanded = false) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  mountedCards.push({ container, root });

  const rerender = (nextSteps: RunStep[]) => {
    act(() => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          {nextSteps.map((step, index) => (
            <div key={`${step.step}-${index}`} data-segment-index={index}>
              <RunStepCard step={step} defaultExpanded={defaultExpanded} />
            </div>
          ))}
        </IntlProvider>,
      );
    });
  };

  rerender(steps);
  return { container, rerender };
}

// ─── File existence ───────────────────────────────────────────────────────────

describe("history/ component files — existence", () => {
  it("RunDetail.tsx exists", () => {
    expect(fs.existsSync(path.join(HISTORY_DIR, "RunDetail.tsx"))).toBe(true);
  });

  it("InvocationDetail.tsx exists", () => {
    expect(fs.existsSync(path.join(HISTORY_DIR, "InvocationDetail.tsx"))).toBe(true);
  });
});

// ─── No Drawer in history components ─────────────────────────────────────────

describe("history/ — no Drawer overlay import (master-detail doctrine §4)", () => {
  const FILES = ["RunDetail.tsx", "InvocationDetail.tsx"];

  for (const file of FILES) {
    it(`${file} does not import Drawer`, () => {
      const src = fs.readFileSync(path.join(HISTORY_DIR, file), "utf-8");
      expect(src).not.toMatch(/import.*Drawer.*from/);
      expect(src).not.toMatch(/from.*shell\/Drawer/);
    });
  }
});

// ─── SSE done-refetch stale-write race guard (MAJ-3) ─────────────────────────
// The 'done' handler refetches status/reason fields after streamSession
// reports completion. Without a same-session guard, navigating A→B before
// A's refetch resolves lets A's data clobber B's freshly-fetched state.

describe("history/RunDetail.tsx — SSE done-refetch is guarded against a stale-session write", () => {
  it("the refetch merge is gated on prev.id matching the fetched session's id", () => {
    const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");
    expect(src).toMatch(/prev\.id === fresh\.id/);
  });

  it("the streamSession effect cancels its refetch on cleanup", () => {
    const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");
    expect(src).toMatch(/cancelled = true/);
  });
});

// ─── fullPage prop removal (dead branch, single live callsite) ────────────────

describe("history/RunDetail.tsx — fullPage prop removed", () => {
  const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");

  it("does not declare a fullPage prop", () => {
    expect(src).not.toMatch(/fullPage/);
  });

  it("does not branch on a full-page vs. pane wrapper mode", () => {
    expect(src).not.toMatch(/if \(fullPage\)/);
  });
});

describe("history/RunDetail.tsx — bounded incremental signal projection", () => {
  const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");

  it("feeds the stream into SignalProjection instead of an ever-growing React array", () => {
    expect(src).toMatch(/const projection = new SignalProjection\(\)/);
    expect(src).toMatch(/projection\.append\(sig\)/);
    expect(src).not.toMatch(/setSignalEvents/);
    expect(src).not.toMatch(/prev\.some\(\(e\) => e\.id === sig\.id\)/);
  });
});

describe("fleet/SessionDetail.tsx — renders RunDetail without fullPage", () => {
  it("passes only id to RunDetail", () => {
    const src = fs.readFileSync(path.resolve(HISTORY_DIR, "../fleet/SessionDetail.tsx"), "utf-8");
    expect(src).toMatch(/<RunDetail id={runId} \/>/);
    expect(src).not.toMatch(/fullPage/);
  });
});

// ─── Authored graph is reduced at display time only ──────────────────────────
// runGraph is Studio's persisted early_graph — the exact graph the designer
// authored, resolved (resolveGraphEdges) but otherwise as wired: it can carry
// one depends_on-style edge per ancestor, same as the runtime opGraph below,
// so it clutters the same way a raw ancestor list does. Unlike opGraph, an
// authored edge can also carry a condition/handler/map/code mode — semantics
// the designer put there on purpose, not structural redundancy. So the
// authored graph IS reduced, but only for display (never mutated/re-persisted)
// and only through transitiveReduceDisplay, whose semantic guard never drops
// a rich edge and whose cycle guard renders everything unchanged if the graph
// isn't a DAG — plain transitiveReduce (used for opGraph) has neither guard.

describe("history/RunDetail.tsx — authored run graph is reduced at display time only", () => {
  const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");

  it("imports the display-time transitiveReduceDisplay, not the runtime transitiveReduce", () => {
    const importBlock = src.match(/import \{[^}]*\} from "@\/lib\/operationGraph";/)?.[0] ?? "";
    expect(importBlock).toMatch(/transitiveReduceDisplay/);
    expect(importBlock).not.toMatch(/\btransitiveReduce\b/);
  });

  it("does not pass runGraph directly to WorkerCanvas — edges go through the reduction first", () => {
    expect(src).not.toMatch(/graph={runGraph}/);
    expect(src).toMatch(/graph=\{\{\s*\.\.\.runGraph,\s*edges:\s*displayEdges\s*\}\}/);
  });
});

describe("transitiveReduceDisplay (lib/operationGraph) — why it's safe to apply to runGraph where plain transitiveReduce was not", () => {
  it("keeps an authored conditional A→C that plain transitiveReduce would drop as redundant via A→B→C", async () => {
    const { transitiveReduce, transitiveReduceDisplay } = await import("@/lib/operationGraph");

    // Mirrors an authored WorkerGraph: A→B, B→C, and a conditional A→C.
    const authoredEdges = [
      { id: "e-ab", source: "A", target: "B" },
      { id: "e-bc", source: "B", target: "C" },
      { id: "e-ac", source: "A", target: "C", condition: "score > 0.8" },
    ];

    // The runtime reducer would drop it: C is reachable from A through B,
    // and it has no notion of "this edge carries a condition".
    const wouldHaveReduced = transitiveReduce(authoredEdges);
    expect(wouldHaveReduced.find((e) => e.id === "e-ac")).toBeUndefined();

    // The display-time reducer RunDetail actually calls keeps it.
    const { kept, hidden } = transitiveReduceDisplay(authoredEdges);
    expect(kept.find((e) => e.id === "e-ac")).toBeDefined();
    expect(hidden).toHaveLength(0);
  });
});

// ─── Reduced-by-default with a show-implied-edges escape hatch ───────────────
// computeDisplayEdges is the pure core of RunDetail's edge-selection useMemo:
// reduce by default (transitiveReduceDisplay), fall back to the full resolved
// set when the toggle is on, and always report how many edges the reduction
// hid so the chrome can show it regardless of which set is currently shown.

describe("computeDisplayEdges (RunDetail) — reduced-by-default, toggle restores the full set", () => {
  const diamondWithSkip: WorkerGraph["edges"] = [
    { id: "e-ab", source: "A", target: "B", mode: "simple" },
    { id: "e-bc", source: "B", target: "C", mode: "simple" },
    { id: "e-ac", source: "A", target: "C", mode: "simple" }, // redundant: A→B→C
  ];

  it("reduces by default and reports the hidden count", async () => {
    const { computeDisplayEdges } = await import("./RunDetail");
    const { displayEdges, hiddenCount } = computeDisplayEdges(diamondWithSkip, false);
    expect(displayEdges).toHaveLength(2);
    expect(displayEdges.find((e) => e.id === "e-ac")).toBeUndefined();
    expect(hiddenCount).toBe(1);
  });

  it("show-implied-edges toggle restores the full resolved set without losing the hidden count", async () => {
    const { computeDisplayEdges } = await import("./RunDetail");
    const { displayEdges, hiddenCount } = computeDisplayEdges(diamondWithSkip, true);
    expect(displayEdges).toHaveLength(3);
    expect(displayEdges.find((e) => e.id === "e-ac")).toBeDefined();
    expect(hiddenCount).toBe(1);
  });

  it("a semantic edge survives reduction — hiddenCount is 0, nothing to toggle", async () => {
    const { computeDisplayEdges } = await import("./RunDetail");
    const withCondition: WorkerGraph["edges"] = [
      { id: "e-ab", source: "A", target: "B", mode: "simple" },
      { id: "e-bc", source: "B", target: "C", mode: "simple" },
      { id: "e-ac", source: "A", target: "C", mode: "simple", condition: "score > 0.8" },
    ];
    const { displayEdges, hiddenCount } = computeDisplayEdges(withCondition, false);
    expect(displayEdges).toHaveLength(3);
    expect(hiddenCount).toBe(0);
  });

  it("empty edges reduce to empty, zero hidden", async () => {
    const { computeDisplayEdges } = await import("./RunDetail");
    expect(computeDisplayEdges([], false)).toEqual({ displayEdges: [], hiddenCount: 0 });
  });
});

// ─── Hidden-count badge + show-implied-edges toggle wired into the chrome ────

describe("history/RunDetail.tsx — hidden-implied-edge count and toggle wired into the run-dag chrome", () => {
  const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");

  it("the run-dag SectionHeader receives edgeCount/hiddenCount/toggle props sourced from the reduction", () => {
    const start = src.indexOf('id="run-dag"');
    const end = src.indexOf("</Suspense>", start);
    const block = src.slice(start, end);
    expect(block).toMatch(/edgeCount=\{displayEdges\.length\}/);
    expect(block).toMatch(/hiddenCount=\{hiddenCount\}/);
    expect(block).toMatch(/onToggleImplied=\{.*setShowImpliedEdges/);
    expect(block).toMatch(/showImplied=\{showImpliedEdges\}/);
  });

  it("SectionHeader only renders the hidden badge/toggle once hiddenCount is positive, and defaults to reduced", () => {
    expect(src).toMatch(/hiddenCount\s*!=\s*null\s*&&\s*hiddenCount\s*>\s*0/);
    expect(src).toMatch(/const \[showImpliedEdges, setShowImpliedEdges\] = useState\(false\)/);
  });
});

// ─── Hidden-count badge + toggle, mounted for real ───────────────────────────
// The two describe blocks above (computeDisplayEdges, and the source-text
// checks on the run-dag SectionHeader call) establish the pure selection
// logic is right and that the JSX wires the right prop names — but neither
// proves the badge text actually renders, that the button actually flips
// which edge set WorkerCanvas receives, or that a graph with nothing hidden
// omits the toggle. This mounts the real RunDetail (getSession/streamSession/
// streamSignals/ResumeRun/WorkerCanvas mocked at module scope, above) against
// a diamond-with-skip graph (A→B→C plus a redundant A→C) and drives the
// button through a real click.

describe("history/RunDetail.tsx — hidden-count badge and show-implied toggle, mounted", () => {
  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    // jsdom does not implement scrollIntoView; RunDetail calls it on load
    // (see RunDetail.pagination.test.tsx, which mounts the same component).
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const diamondWithSkipGraph = {
    name: "run",
    description: "",
    nodes: [
      {
        id: "A",
        label: "A",
        role: "",
        assignment: "",
        prompt: "",
        capacity: 1,
        timeout: null,
        inputs: [],
        outputs: [],
      },
      {
        id: "B",
        label: "B",
        role: "",
        assignment: "",
        prompt: "",
        capacity: 1,
        timeout: null,
        inputs: [],
        outputs: [],
      },
      {
        id: "C",
        label: "C",
        role: "",
        assignment: "",
        prompt: "",
        capacity: 1,
        timeout: null,
        inputs: [],
        outputs: [],
      },
    ],
    edges: [
      { id: "e-ab", source: "A", target: "B", mode: "simple" as const },
      { id: "e-bc", source: "B", target: "C", mode: "simple" as const },
      { id: "e-ac", source: "A", target: "C", mode: "simple" as const }, // redundant: A→B→C
    ],
  };

  const minimalSession = (graph: unknown) => ({
    id: "run-mount-1",
    name: "run-mount-1",
    created_at: 0,
    updated_at: 0,
    status: "completed",
    branches: [],
    graph,
  });

  async function mountRunDetail(graph: unknown) {
    const [{ getSession }, { default: RunDetail }] = await Promise.all([
      import("@/lib/api"),
      import("./RunDetail"),
    ]);
    vi.mocked(getSession).mockResolvedValue(minimalSession(graph) as never);

    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <RunDetail id="run-mount-1" />
        </IntlProvider>,
      );
    });
    // getSession resolves asynchronously and lazy(WorkerCanvas) suspends for
    // at least one microtask; flush both before asserting.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    return {
      container,
      unmount: () => {
        act(() => root.unmount());
        container.remove();
      },
    };
  }

  it("shows the hidden-count badge and the reduced edge set by default", async () => {
    const { container, unmount } = await mountRunDetail(diamondWithSkipGraph);
    try {
      expect(container.textContent).toContain("1 implied hidden");
      const canvas = container.querySelector('[data-testid="worker-canvas"]');
      expect(canvas?.getAttribute("data-edge-count")).toBe("2");
      const toggle = Array.from(container.querySelectorAll("button")).find(
        (b) => b.textContent === "show implied edges",
      );
      expect(toggle).toBeDefined();
    } finally {
      unmount();
    }
  });

  it("clicking the toggle flips the button label and hands WorkerCanvas the full edge set", async () => {
    const { container, unmount } = await mountRunDetail(diamondWithSkipGraph);
    try {
      const toggle = Array.from(container.querySelectorAll("button")).find(
        (b) => b.textContent === "show implied edges",
      );
      expect(toggle).toBeDefined();

      await act(async () => {
        toggle?.click();
      });
      await act(async () => {
        await Promise.resolve();
      });

      const canvasAfter = container.querySelector('[data-testid="worker-canvas"]');
      expect(canvasAfter?.getAttribute("data-edge-count")).toBe("3");
      const hideButton = Array.from(container.querySelectorAll("button")).find(
        (b) => b.textContent === "hide implied",
      );
      expect(hideButton).toBeDefined();
      // The badge count itself must not change on toggle — 1 edge is still
      // implied, whichever set is currently shown.
      expect(container.textContent).toContain("1 implied hidden");
    } finally {
      unmount();
    }
  });

  it("an already-minimal graph (nothing hidden) renders no badge and no toggle", async () => {
    const minimalGraph = {
      name: "run",
      description: "",
      nodes: [
        {
          id: "A",
          label: "A",
          role: "",
          assignment: "",
          prompt: "",
          capacity: 1,
          timeout: null,
          inputs: [],
          outputs: [],
        },
        {
          id: "B",
          label: "B",
          role: "",
          assignment: "",
          prompt: "",
          capacity: 1,
          timeout: null,
          inputs: [],
          outputs: [],
        },
      ],
      edges: [{ id: "e-ab", source: "A", target: "B", mode: "simple" as const }],
    };
    const { container, unmount } = await mountRunDetail(minimalGraph);
    try {
      expect(container.textContent).not.toContain("implied hidden");
      const toggle = Array.from(container.querySelectorAll("button")).find(
        (b) => b.textContent === "show implied edges" || b.textContent === "hide implied",
      );
      expect(toggle).toBeUndefined();
      const canvas = container.querySelector('[data-testid="worker-canvas"]');
      expect(canvas?.getAttribute("data-edge-count")).toBe("1");
    } finally {
      unmount();
    }
  });
});

// ─── Edgeless authored graph falls through to the runtime opGraph ────────────
// Reactive runs persist an early `graph` snapshot (nodes only, no edges yet)
// that is never refreshed. Laid out with zero edges, dagre puts every node
// in the same rank — a meaningless vertical column. When that snapshot has
// ≥2 nodes and 0 edges, and the runtime opGraph (built from Node* signal
// depends_on/parent_id/cause_op_id) has real edges, the authored graph must
// not be rendered as the DAG — render opGraph instead. An authored graph
// that already carries edges keeps priority exactly as before.

describe("history/RunDetail.tsx — shouldRenderAuthoredGraph", () => {
  it("exports shouldRenderAuthoredGraph and wires it into the run-dag render branch", () => {
    const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");
    expect(src).toMatch(/export function shouldRenderAuthoredGraph/);
    expect(src).toMatch(/runGraph && shouldRenderAuthoredGraph\(runGraph, opGraph\)/);
  });

  it("passes compact to the authored-graph WorkerCanvas embed", () => {
    const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");
    // The <WorkerCanvas ... compact /> block sits between the authored-graph
    // ternary head and the opGraph fallback branch.
    const start = src.indexOf("shouldRenderAuthoredGraph(runGraph, opGraph)");
    const end = src.indexOf("</Suspense>", start);
    expect(src.slice(start, end)).toMatch(/\bcompact\b/);
  });

  it("edgeless authored graph + runtime edges → opGraph path chosen", async () => {
    const { shouldRenderAuthoredGraph } = await import("./RunDetail");
    const authoredNoEdges = {
      nodes: [{ id: "a" }, { id: "b" }],
      edges: [],
    };
    const opGraphWithEdges = { edges: [{ source: "op-a", target: "op-b" }] };
    expect(shouldRenderAuthoredGraph(authoredNoEdges, opGraphWithEdges)).toBe(false);
  });

  it("edgeless authored graph but opGraph ALSO has no edges → still renders authored (nothing better to fall through to)", async () => {
    const { shouldRenderAuthoredGraph } = await import("./RunDetail");
    const authoredNoEdges = { nodes: [{ id: "a" }, { id: "b" }], edges: [] };
    expect(shouldRenderAuthoredGraph(authoredNoEdges, { edges: [] })).toBe(true);
  });

  it("authored graph WITH edges is still preferred over opGraph, regardless of opGraph edges", async () => {
    const { shouldRenderAuthoredGraph } = await import("./RunDetail");
    const authoredWithEdges = {
      nodes: [{ id: "a" }, { id: "b" }],
      edges: [{ id: "e1", source: "a", target: "b" }],
    };
    const opGraphWithEdges = { edges: [{ source: "op-a", target: "op-b" }] };
    expect(shouldRenderAuthoredGraph(authoredWithEdges, opGraphWithEdges)).toBe(true);
    expect(shouldRenderAuthoredGraph(authoredWithEdges, { edges: [] })).toBe(true);
  });

  it("missing graph.edges (backend omitted the field) is treated as edgeless", async () => {
    const { shouldRenderAuthoredGraph } = await import("./RunDetail");
    const authoredMissingEdges = {
      nodes: [{ id: "a" }, { id: "b" }],
      edges: undefined as unknown as unknown[],
    };
    const opGraphWithEdges = { edges: [{ source: "op-a", target: "op-b" }] };
    expect(shouldRenderAuthoredGraph(authoredMissingEdges, opGraphWithEdges)).toBe(false);
  });

  // This assertion is inverted from what it first said, on purpose. It read
  // "a single-node authored graph is never considered edgeless, because there
  // is nothing to draw an edge between" — which is true of that snapshot on
  // its own, and the wrong rule for deciding which source to draw from. Held
  // as authoritative, a one-node snapshot sitting beside a runtime graph that
  // does have an edge meant the real DAG was never rendered at all.
  it("a single-node authored graph yields to a runtime graph that has edges", async () => {
    const { shouldRenderAuthoredGraph } = await import("./RunDetail");
    const singleNode = { nodes: [{ id: "a" }], edges: [] };
    const opGraphWithEdges = { edges: [{ source: "op-a", target: "op-b" }] };
    expect(shouldRenderAuthoredGraph(singleNode, opGraphWithEdges)).toBe(false);
  });

  // The other arm, which the inversion must not cost: with nothing to fall
  // through to, the authored snapshot is still what renders.
  it("a single-node authored graph still renders when the runtime graph has no edges either", async () => {
    const { shouldRenderAuthoredGraph } = await import("./RunDetail");
    const singleNode = { nodes: [{ id: "a" }], edges: [] };
    expect(shouldRenderAuthoredGraph(singleNode, { edges: [] })).toBe(true);
  });

  it("null graph never renders as the authored DAG", async () => {
    const { shouldRenderAuthoredGraph } = await import("./RunDetail");
    expect(shouldRenderAuthoredGraph(null, { edges: [] })).toBe(false);
  });

  // A persisted graph may omit `edges` entirely. shouldRenderAuthoredGraph
  // treats that as edgeless, but when the runtime opGraph ALSO has no edges
  // the authored graph still renders — and WorkerCanvas maps over `edges`,
  // so the decode site must normalize an omitted field to [] or that valid
  // combination crashes the run-detail graph instead of rendering it.
  it("decode site resolves graph.edges (numeric-ref repair + omitted → []) before setRunGraph", () => {
    const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");
    // resolveGraphEdges handles both concerns: null/undefined edges become []
    // and planner step-number refs are mapped onto node ids.
    expect(src).toMatch(/edges:\s*resolveGraphEdges\(graph\.nodes,\s*graph\.edges\)/);
  });

  // The progress counters are sentence-case labels ("Total", "Completed"); the
  // graphNodeStatus* vocabulary is lowercase because it renders inside a node
  // card ("done", "running"). The escalated counter borrowed the node-status
  // key, so it rendered as the only lowercase label in the strip. Assert on the
  // source because the counters carry no other behaviour to observe.
  it("the escalated counter uses the progress label vocabulary, not the node-status one", () => {
    const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");
    expect(src).toMatch(/\{t\("progressEscalated"\)\}\s*\{counts\.escalated\}/);
    expect(src).not.toMatch(/\{t\("graphNodeStatusEscalated"\)\}\s*\{counts\.escalated\}/);
  });

  it("omitted edges + no runtime edges renders the authored graph, and normalized edges survive a WorkerCanvas-style map", async () => {
    const { shouldRenderAuthoredGraph } = await import("./RunDetail");
    const persisted = { nodes: [{ id: "a" }, { id: "b" }] } as {
      nodes: unknown[];
      edges?: unknown[] | null;
    };
    // Mirrors the decode-site normalization under test above.
    const runGraph = { nodes: persisted.nodes, edges: persisted.edges ?? [] };
    expect(shouldRenderAuthoredGraph(runGraph, { edges: [] })).toBe(true);
    expect(() => runGraph.edges.map((e) => e)).not.toThrow();
  });
});

// ─── runFiles seeds from the server's full-session file union ────────────────
// Sessions are windowed to SESSION_MESSAGE_PAGE (200) messages (lib/api.ts).
// A step's own messages therefore cannot resolve a file reference that was
// touched earlier in a long session — the server already computes the full
// union over every branch's whole progression (services/sessions.py
// _branch_message_stats -> get_session's message_stats.files) and returns it
// on SessionDetail. runFiles must seed from that surface, not just the
// loaded steps.

describe("history/RunDetail.tsx — runFiles seeds from session.message_stats.files", () => {
  const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");

  it("unions the server-side full-session file surface into runFiles", () => {
    expect(src).toMatch(/session\?\.message_stats\?\.files/);
  });

  it("runFiles depends on session, not steps alone, so a server-only update refreshes it", () => {
    const start = src.indexOf("const runFiles = useMemo(");
    const end = src.indexOf(";", src.indexOf("}, [", start));
    const block = src.slice(start, end);
    expect(block).toMatch(/\[steps, session\]/);
  });
});

describe("runFiles union logic (mirrors the useMemo body) — file outside the loaded window resolves", () => {
  // Mirrors: const set = new Set(session?.message_stats?.files ?? []);
  //          for (const step of steps) for (const p of extractFilePaths(...)) set.add(p);
  function computeRunFiles(
    serverFiles: string[] | undefined,
    stepDerivedFiles: string[],
  ): string[] {
    const set = new Set<string>(serverFiles ?? []);
    for (const p of stepDerivedFiles) set.add(p);
    return Array.from(set);
  }

  it("includes a file only present in the server's full-session union (touched before the 200-message tail window)", () => {
    const serverUnion = ["consolidatedfixspec.md", "review.md"]; // computed over the FULL progression
    const loadedStepFiles = ["review.md"]; // only what's in the windowed tail
    const result = computeRunFiles(serverUnion, loadedStepFiles);
    expect(result).toContain("consolidatedfixspec.md");
    expect(result).toContain("review.md");
  });

  it("still includes client-derived files the server union happens to miss (defensive union, not a replacement)", () => {
    const result = computeRunFiles(["a.md"], ["b.md"]);
    expect(result.sort()).toEqual(["a.md", "b.md"]);
  });

  it("degrades gracefully when message_stats is absent (older/partial session payloads)", () => {
    const result = computeRunFiles(undefined, ["c.md"]);
    expect(result).toEqual(["c.md"]);
  });
});

describe("history/RunDetail.tsx — persisted branch totals survive message pagination", () => {
  it("uses full-progression timestamps and message totals instead of the loaded tail", async () => {
    const { branchToRunStep } = await import("./RunDetail");
    const runStep = branchToRunStep(
      {
        id: "branch-1",
        name: "worker",
        created_at: 10,
        first_message_at: 10,
        last_message_at: 610,
        message_total: 30_525,
        messages: [
          {
            id: "recent-1",
            role: "assistant",
            content: { assistant_response: "tail" },
            sender: "worker",
            timestamp: 600,
            lion_class: "AssistantResponse",
          },
          {
            id: "recent-2",
            role: "assistant",
            content: { assistant_response: "tail end" },
            sender: "worker",
            timestamp: 610,
            lion_class: "AssistantResponse",
          },
        ],
      },
      "completed",
    );

    expect(runStep.result?.duration_sec).toBe(600);
    expect(runStep.result?.message_count).toBe(30_525);
  });

  it("uses branch lifecycle bounds when a cancelled branch has only one message", async () => {
    const { branchToRunStep } = await import("./RunDetail");
    const runStep = branchToRunStep(
      {
        id: "branch-cancelled",
        name: "architect",
        created_at: 100,
        started_at: null,
        ended_at: 178.1,
        first_message_at: 101,
        last_message_at: 101,
        message_total: 1,
        messages: [
          {
            id: "prompt-only",
            role: "user",
            content: { instruction: "Investigate" },
            sender: "user",
            timestamp: 101,
            lion_class: "Instruction",
          },
        ],
      },
      "cancelled",
    );

    expect(runStep.result?.duration_sec).toBe(78);
    const { container } = renderRunStepCards([runStep]);
    expect(container.textContent).toContain("1m 18s");
    expect(container.textContent).not.toContain("0s");
  });
});

describe("history/RunDetail.tsx — live branch aggregates", () => {
  it("refreshes the rendered memoized card duration after a terminal refetch", () => {
    const messages = [
      {
        role: "assistant",
        content: "finished",
        sender: "worker",
        timestamp: 20,
      },
    ];
    const runningStep: RunStep = {
      step: "worker",
      status: "completed",
      timestamp: 10,
      messages,
      result: { agent: "worker", message_count: 1, duration_sec: 10 },
    };
    const terminalStep: RunStep = {
      ...runningStep,
      messages,
      result: { ...runningStep.result, duration_sec: 50 },
    };
    const { container, rerender } = renderRunStepCards([runningStep]);

    expect(container.textContent).toContain("10s");

    rerender([terminalStep]);

    expect(container.textContent).not.toContain("10s");
    expect(container.textContent).toContain("50s");
  });

  it("renders a streamed message once and advances duration through the terminal refetch", async () => {
    const { appendStreamedMessage, branchToRunStep, mergeCompletedSession } =
      await import("./RunDetail");
    const initial = {
      id: "run-1",
      name: "run",
      created_at: 10,
      updated_at: 20,
      status: "running",
      branches: [
        {
          id: "branch-1",
          name: "worker",
          created_at: 10,
          first_message_at: 10,
          last_message_at: 20,
          message_total: 2,
          messages: [
            {
              id: "older-1",
              role: "assistant",
              content: { assistant_response: "oldest loaded" },
              sender: "worker",
              timestamp: 10,
              lion_class: "AssistantResponse",
            },
            {
              id: "initial-tail",
              role: "assistant",
              content: { assistant_response: "initial tail" },
              sender: "worker",
              timestamp: 20,
              lion_class: "AssistantResponse",
            },
          ],
        },
      ],
    };
    const streamedMessage = {
      id: "streamed-later",
      role: "assistant",
      branch_id: "branch-1",
      content: { assistant_response: "live" },
      sender: "worker",
      timestamp: 50,
      lion_class: "AssistantResponse",
    };

    const afterFirstEvent = appendStreamedMessage(initial, "branch-1", streamedMessage);
    const afterDuplicateEvent = appendStreamedMessage(afterFirstEvent, "branch-1", streamedMessage);
    const firstStep = branchToRunStep(afterFirstEvent.branches[0], "running");
    const duplicateStep = branchToRunStep(afterDuplicateEvent.branches[0], "running");
    const { container, rerender } = renderRunStepCards([firstStep], true);
    const conversationBadge = () =>
      container.querySelector('[id$="-tab-conversation"] span')?.textContent;
    const renderedDuration = () =>
      Array.from(container.querySelectorAll<HTMLElement>("#step-worker > button span"))
        .map((element) => element.textContent)
        .find((text) => /^(?:\d+m )?\d+s$/.test(text ?? ""));
    const renderedLiveResponses = () =>
      Array.from(container.querySelectorAll<HTMLElement>('[id^="step-worker-r"]')).filter(
        (response) => response.textContent?.includes("live"),
      );
    const conversationTab = container.querySelector<HTMLButtonElement>(
      '[role="tab"][id$="-tab-conversation"]',
    );

    expect(conversationTab).not.toBeNull();
    await act(async () => conversationTab?.click());

    const firstBadge = conversationBadge();
    const firstDuration = renderedDuration();
    expect(renderedLiveResponses()).toHaveLength(1);
    expect(firstBadge).toBe("3");
    expect(firstDuration).toBe("40s");

    rerender([duplicateStep]);

    expect(renderedLiveResponses()).toHaveLength(1);
    expect(conversationBadge()).toBe(firstBadge);
    expect(renderedDuration()).toBe(firstDuration);

    const completed = mergeCompletedSession(afterDuplicateEvent, {
      ...initial,
      status: "completed",
      updated_at: 60,
      ended_at: 60,
      branches: [
        {
          ...initial.branches[0],
          last_message_at: 60,
          message_total: 4,
          messages: [
            {
              id: "terminal-tail",
              role: "assistant",
              content: { assistant_response: "done" },
              sender: "worker",
              timestamp: 60,
              lion_class: "AssistantResponse",
            },
          ],
        },
      ],
    });
    const completedStep = branchToRunStep(completed.branches[0], "completed");

    expect(completedStep.result?.duration_sec).toBe(50);
    expect(completedStep.result?.message_count).toBe(4);
    expect(completed.branches[0].messages.map((message) => message.id)).toEqual([
      "older-1",
      "initial-tail",
      "streamed-later",
      "terminal-tail",
    ]);
  });

  it("rejects a raw SSE event whose timestamp is not a number before it is cast to SessionMessage", async () => {
    const { isSessionMessageEvent } = await import("./RunDetail");

    const malformed: Record<string, unknown> = {
      id: "streamed-later",
      role: "assistant",
      branch_id: "branch-1",
      content: { assistant_response: "live" },
      sender: "worker",
      timestamp: null,
      lion_class: "AssistantResponse",
    };
    const wellFormed: Record<string, unknown> = { ...malformed, timestamp: 50 };

    expect(isSessionMessageEvent(malformed)).toBe(false);
    expect(isSessionMessageEvent(wellFormed)).toBe(true);
  });
});

describe("history/RunDetail.tsx — segmented branch totals", () => {
  it("omits intermediate window counts and shows the persisted branch total only on the final segment", async () => {
    const { buildRunSteps } = await import("./RunDetail");
    const steps = buildRunSteps(
      {
        id: "run-1",
        name: "run",
        created_at: 0,
        updated_at: 200,
        branches: [
          {
            id: "branch-1",
            name: "worker",
            created_at: 0,
            first_message_at: 10,
            last_message_at: 190,
            message_total: 6,
            messages: [
              {
                id: "loaded-from-first-segment",
                role: "assistant",
                content: { assistant_response: "first segment tail" },
                sender: "worker",
                timestamp: 90,
                lion_class: "AssistantResponse",
              },
              {
                id: "loaded-from-final-segment",
                role: "assistant",
                content: { assistant_response: "final segment tail" },
                sender: "worker",
                timestamp: 190,
                lion_class: "AssistantResponse",
              },
            ],
          },
        ],
      },
      "completed",
      [
        {
          op_id: "op-1",
          branch_id: "branch-1",
          branch_name: "worker",
          status: "completed",
          started_at: 0,
          ended_at: 99,
        },
        {
          op_id: "op-2",
          branch_id: "branch-1",
          branch_name: "worker",
          status: "completed",
          started_at: 100,
          ended_at: 200,
        },
      ],
    );

    expect(steps).toHaveLength(2);
    expect(steps[0].messages).toHaveLength(1);
    expect(steps[0].result?.message_count).toBeNull();
    expect(steps[1].messages).toHaveLength(1);
    expect(steps[1].result?.message_count).toBe(6);

    const { container } = renderRunStepCards(steps, true);
    const cards = container.querySelectorAll<HTMLElement>("[data-segment-index]");
    const intermediateBadge = cards[0]?.querySelector('[id$="-tab-conversation"] span');
    const finalBadge = cards[1]?.querySelector('[id$="-tab-conversation"] span');

    expect(intermediateBadge).toBeNull();
    expect(finalBadge?.textContent).toBe("6");
  });
});

describe("history/RunDetail.tsx — overview aggregates are lifetime totals", () => {
  it("prefers full-session aggregate counts to the loaded message window", async () => {
    const { resolveOverviewCounts } = await import("./RunDetail");
    expect(
      resolveOverviewCounts(
        {
          message_count: 30_525,
          roles: {},
          tool_call_count: 21_741,
          error_count: 42,
          files: [],
        },
        { toolCallCount: 2, errorCount: 1 },
      ),
    ).toEqual({ toolCallCount: 21_741, errorCount: 42, countsAreFloors: false });
  });

  it("reports the counts as floors when the server says its pass was bounded", async () => {
    const { resolveOverviewCounts } = await import("./RunDetail");
    expect(
      resolveOverviewCounts(
        {
          message_count: 30_525,
          roles: {},
          tool_call_count: 21_741,
          error_count: 42,
          files: [],
          bounded: true,
        },
        { toolCallCount: 2, errorCount: 1 },
      ),
    ).toEqual({ toolCallCount: 21_741, errorCount: 42, countsAreFloors: true });
  });

  async function mountOverview(messageStats: Record<string, unknown>) {
    const [{ getSession }, { default: RunDetail }] = await Promise.all([
      import("@/lib/api"),
      import("./RunDetail"),
    ]);
    vi.mocked(getSession).mockResolvedValue({
      id: "run-overview-labels",
      name: "run-overview-labels",
      created_at: 0,
      updated_at: 0,
      status: "completed",
      branches: [],
      message_stats: messageStats,
    } as never);

    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <RunDetail id="run-overview-labels" />
        </IntlProvider>,
      );
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    return {
      container,
      unmount: () => {
        act(() => root.unmount());
        container.remove();
      },
    };
  }

  const FULL_STATS = {
    message_count: 30_525,
    roles: {},
    tool_call_count: 21_741,
    error_count: 0,
    files: [],
  };

  it("labels the count tiles as totals when the server read the whole surface", async () => {
    const { container, unmount } = await mountOverview(FULL_STATS);
    try {
      // Control for the assertion below: the unqualified labels have to be
      // reachable, or finding the qualified ones proves nothing.
      expect(container.textContent).toContain("Tool calls");
      expect(container.textContent).not.toContain("Tool calls (recent)");
      expect(container.textContent).not.toContain("Errors (recent)");
    } finally {
      unmount();
    }
  });

  it("qualifies the count tiles as recent when the server's pass was bounded", async () => {
    const { container, unmount } = await mountOverview({ ...FULL_STATS, bounded: true });
    try {
      // The counts came from the newest slice of a long session's action rows,
      // so they are floors. Under the plain label a floor reads as a total,
      // and a zero error count reads as a clean run.
      expect(container.textContent).toContain("Tool calls (recent)");
      expect(container.textContent).toContain("Errors (recent)");
    } finally {
      unmount();
    }
  });
});

// ─── Files section: a cut union says so ───────────────────────────────────────
// The server stops the run-wide file union at a ceiling and reports that it
// did. A cut union reaches this section in two shapes -- a short list and an
// empty one -- and both of them read as a complete answer unless the note is
// rendered, so each shape gets its own arm and its own control.

describe("history/RunDetail.tsx — the files section discloses a cut union", () => {
  async function mountFiles(messageStats: Record<string, unknown>) {
    const [{ getSession }, { default: RunDetail }] = await Promise.all([
      import("@/lib/api"),
      import("./RunDetail"),
    ]);
    vi.mocked(getSession).mockResolvedValue({
      id: "run-files-note",
      name: "run-files-note",
      created_at: 0,
      updated_at: 0,
      status: "completed",
      branches: [],
      message_stats: messageStats,
    } as never);

    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <RunDetail id="run-files-note" />
        </IntlProvider>,
      );
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    return {
      container,
      unmount: () => {
        act(() => root.unmount());
        container.remove();
      },
    };
  }

  const CUT_NOTE = "this run touched more files than one view collects";
  const STATS = {
    message_count: 4,
    roles: {},
    tool_call_count: 4,
    error_count: 0,
  };

  it("says the list is short when the union was cut with names in it", async () => {
    const { container, unmount } = await mountFiles({
      ...STATS,
      files: ["/run/a.py", "/run/b.py"],
      files_bounded: true,
    });
    try {
      expect(container.textContent).toContain("/run/b.py");
      expect(container.textContent).toContain(CUT_NOTE);
    } finally {
      unmount();
    }
  });

  it("leaves the same list unqualified when the union was complete", async () => {
    const { container, unmount } = await mountFiles({
      ...STATS,
      files: ["/run/a.py", "/run/b.py"],
      files_bounded: false,
    });
    try {
      expect(container.textContent).toContain("/run/b.py");
      expect(container.textContent).not.toContain(CUT_NOTE);
    } finally {
      unmount();
    }
  });

  it("says the same about an empty list, where a complete answer means no files", async () => {
    const { container, unmount } = await mountFiles({
      ...STATS,
      files: [],
      files_bounded: true,
    });
    try {
      expect(container.textContent).toContain("No file operations detected");
      expect(container.textContent).toContain(CUT_NOTE);
    } finally {
      unmount();
    }
  });

  it("leaves an empty list unqualified when the union was complete", async () => {
    const { container, unmount } = await mountFiles({
      ...STATS,
      files: [],
      files_bounded: false,
    });
    try {
      expect(container.textContent).toContain("No file operations detected");
      expect(container.textContent).not.toContain(CUT_NOTE);
    } finally {
      unmount();
    }
  });
});

// ─── NodeEscalated badge tone ─────────────────────────────────────────────────
// Every escalation route is attention-worthy rather than failed. Soft notify
// keeps its distinct label, while hard and legacy escalation shapes retain the
// escalated label; a real NodeFailed remains the error-tone control.

describe("history/RunDetail.tsx — badgeForEvent escalation presentation", () => {
  it("uses warning for every escalation shape while genuine failure stays error-toned", async () => {
    const { badgeForEvent } = await import("./RunDetail");
    const escalationCases = [
      [{ route: "notify" }, "notify"],
      [{ route: "higher_tier" }, "escalated"],
      [{}, "escalated"],
    ] as const;

    for (const [payload, label] of escalationCases) {
      const badge = badgeForEvent({
        id: `escalated-${label}`,
        session_id: "s1",
        seq: 0,
        kind: "NodeEscalated",
        op_id: "op-escalated",
        ts: 1,
        payload,
      });
      expect(badge.label).toBe(label);
      expect(badge.tone).toMatch(/warning/);
      expect(badge.tone).not.toMatch(/error/);
    }

    const failed = badgeForEvent({
      id: "failed",
      session_id: "s1",
      seq: 1,
      kind: "NodeFailed",
      op_id: "op-failed",
      ts: 2,
      payload: {},
    });
    expect(failed.label).toBe("failed");
    expect(failed.tone).toMatch(/error/);
    expect(failed.tone).not.toMatch(/warning/);
  });
});

describe("history/RunDetail.tsx — cancellation presentation", () => {
  it("labels NodeCancelled explicitly without the failure tone", async () => {
    const { badgeForEvent } = await import("./RunDetail");
    const badge = badgeForEvent({
      id: "cancel-1",
      session_id: "session-1",
      seq: 2,
      kind: "NodeCancelled",
      op_id: "op-1",
      ts: 2,
      payload: { name: "work" },
    });

    expect(badge.label).toBe("cancelled");
    expect(badge.tone).toContain("status-warning");
    expect(badge.tone).not.toContain("status-error");
  });
});

describe("history/RunDetail.tsx — operation lane escalation presentation", () => {
  it("uses a warning lane for escalated while a failed lane stays error-toned", async () => {
    const { EventsSection } = await import("./RunDetail");
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    try {
      act(() => {
        root.render(
          <IntlProvider locale="en" messages={enMessages}>
            <EventsSection
              live={false}
              events={[
                sig({
                  id: "escalated",
                  kind: "NodeEscalated",
                  op_id: "op-escalated",
                  payload: { route: "higher_tier" },
                }),
                sig({ id: "failed", kind: "NodeFailed", op_id: "op-failed", payload: {} }),
              ]}
            />
          </IntlProvider>,
        );
      });

      const laneSummary = Array.from(container.querySelectorAll("#run-events > div")).find((div) =>
        div.className.includes("gap-1.5"),
      );
      const laneBadges = Array.from(laneSummary?.querySelectorAll("span") ?? []);
      const escalated = laneBadges.find((span) => span.textContent === "escalated");
      const failed = laneBadges.find((span) => span.textContent === "failed");

      expect(escalated?.className).toContain("text-status-warning");
      expect(escalated?.className).not.toMatch(/status-error/);
      expect(failed?.className).toContain("text-status-error");
      expect(failed?.className).not.toMatch(/status-warning/);
    } finally {
      act(() => root.unmount());
      container.remove();
      vi.unstubAllGlobals();
    }
  });
});

// ─── visibleEventPayloadEntries / summarizeHookEvent — #2862 ─────────────────
// Element/Signal attach created_at/metadata/schema_version to every signal
// row; the events panel must not dump them into the one-line summary, and a
// HookSignal row must read as a human summary, not a struct.

function sig(overrides: Partial<import("@/lib/api").SignalEvent> = {}) {
  return {
    id: "e1",
    session_id: "s1",
    seq: 1,
    kind: "HookSignal",
    op_id: "op-a",
    ts: 1000,
    payload: {},
    ...overrides,
  } as import("@/lib/api").SignalEvent;
}

describe("history/RunDetail.tsx — visibleEventPayloadEntries", () => {
  it("drops op_id, schema_version, and created_at from the visible entries", async () => {
    const { visibleEventPayloadEntries } = await import("./RunDetail");
    const entries = visibleEventPayloadEntries({
      op_id: "op-a",
      schema_version: 1,
      created_at: 1786034040.25,
      name: "step1",
    });
    expect(entries).toEqual([["name", "step1"]]);
  });

  it("drops empty metadata but keeps non-empty metadata", async () => {
    const { visibleEventPayloadEntries } = await import("./RunDetail");
    expect(visibleEventPayloadEntries({ metadata: {} })).toEqual([]);
    expect(visibleEventPayloadEntries({ metadata: { k: "v" } })).toEqual([
      ["metadata", { k: "v" }],
    ]);
  });

  it("returns [] for an undefined payload", async () => {
    const { visibleEventPayloadEntries } = await import("./RunDetail");
    expect(visibleEventPayloadEntries(undefined)).toEqual([]);
  });
});

describe("history/RunDetail.tsx — refShortId", () => {
  it("shortens a value whose whole content is one id", async () => {
    const { refShortId } = await import("./RunDetail");
    expect(refShortId({ id: "0618e931-21c5-4bee-897d-c58bfb273abc" })).toBe("0618e931");
  });

  it("leaves a value alone when shortening it would drop a sibling field", async () => {
    const { refShortId } = await import("./RunDetail");
    expect(refShortId({ id: "0618e931-21c5", role: "assistant" })).toBeNull();
    expect(refShortId({ ref: "0618e931-21c5" })).toBeNull();
  });

  it("declines anything that is not a plain single-id object", async () => {
    const { refShortId } = await import("./RunDetail");
    expect(refShortId([{ id: "a" }])).toBeNull();
    expect(refShortId({})).toBeNull();
    expect(refShortId({ id: 42 })).toBeNull();
    expect(refShortId({ id: "" })).toBeNull();
    expect(refShortId(null)).toBeNull();
    expect(refShortId("0618e931")).toBeNull();
  });
});

describe("history/RunDetail.tsx — compactValue", () => {
  it("renders a message row's ref as a short id instead of a JSON dump", async () => {
    // The payload every MessageAdded row carries, and the only content it
    // has: dumped as JSON it fills the line and truncates mid-id, so a page
    // of these rows reads as one string repeated.
    const { compactValue } = await import("./RunDetail");
    expect(compactValue({ id: "0618e931-21c5-4bee-897d-c58bfb273abc" })).toBe("0618e931");
  });

  it("still dumps a value that carries more than an id", async () => {
    const { compactValue } = await import("./RunDetail");
    expect(compactValue({ session_id: "abc", point: "api.pre_call" })).toBe(
      '{"session_id":"abc","point":"api.pre_call"}',
    );
  });

  it("leaves scalars and nullish values as they were", async () => {
    const { compactValue } = await import("./RunDetail");
    expect(compactValue("api.stream_chunk")).toBe("api.stream_chunk");
    expect(compactValue(42)).toBe("42");
    expect(compactValue(null)).toBe("");
    expect(compactValue(undefined)).toBe("");
  });
});

describe("history/RunDetail.tsx — summarizeHookEvent", () => {
  it("summarizes a tool.pre hook as 'point · tool_name'", async () => {
    const { summarizeHookEvent } = await import("./RunDetail");
    const summary = summarizeHookEvent(
      sig({
        kind: "HookSignal",
        payload: { point: "tool.pre", kwargs: { tool_name: "read_file", call_id: "c1" } },
      }),
    );
    expect(summary).toBe("tool.pre · read_file");
  });

  it("falls back to the bare point when kwargs has no recognized field", async () => {
    const { summarizeHookEvent } = await import("./RunDetail");
    const summary = summarizeHookEvent(
      sig({ kind: "HookSignal", payload: { point: "session.start", kwargs: {} } }),
    );
    expect(summary).toBe("session.start");
  });

  it("returns null for a non-hook signal", async () => {
    const { summarizeHookEvent } = await import("./RunDetail");
    expect(summarizeHookEvent(sig({ kind: "NodeStarted", payload: { name: "step1" } }))).toBeNull();
  });

  it("returns null when a HookSignal payload has no point", async () => {
    const { summarizeHookEvent } = await import("./RunDetail");
    expect(summarizeHookEvent(sig({ kind: "HookSignal", payload: {} }))).toBeNull();
  });
});

// ─── deriveGateOutcome — #2863 ────────────────────────────────────────────────
// A gate/review step's structured verdict is a different population from
// runtime tool errors; deriveGateOutcome scans the signal stream for it so
// the page can surface "Gate: approve-with-fixes · 1 major, 5 minor" beside
// the (possibly zero) runtime-error count instead of letting the green
// "no errors" text read as the run's overall verdict.

describe("history/RunDetail.tsx — deriveGateOutcome", () => {
  it("returns null when no StructuredOutput signal carries a verdict shape", async () => {
    const { deriveGateOutcome } = await import("./RunDetail");
    expect(
      deriveGateOutcome([sig({ kind: "NodeStarted", payload: { name: "step1" } })]),
    ).toBeNull();
  });

  it("extracts verdict and major/minor counts from a review-shaped StructuredOutput", async () => {
    const { deriveGateOutcome } = await import("./RunDetail");
    const outcome = deriveGateOutcome([
      sig({
        kind: "StructuredOutput",
        payload: {
          data: {
            gate_verdict: "approve-with-fixes",
            findings: [
              { severity: "high", description: "a" },
              { severity: "medium", description: "b" },
              { severity: "low", description: "c" },
            ],
          },
        },
      }),
    ]);
    expect(outcome).toEqual({
      verdict: "approve-with-fixes",
      major: 1,
      minor: 2,
      hasFindings: true,
    });
  });

  it("extracts a boolean gate_passed shape with no findings breakdown", async () => {
    const { deriveGateOutcome } = await import("./RunDetail");
    const outcome = deriveGateOutcome([
      sig({ kind: "StructuredOutput", payload: { data: { gate_passed: false } } }),
    ]);
    expect(outcome).toEqual({ verdict: "reject", major: 0, minor: 0, hasFindings: false });
  });

  it("uses the most recent verdict when multiple StructuredOutput signals carry one", async () => {
    const { deriveGateOutcome } = await import("./RunDetail");
    const outcome = deriveGateOutcome([
      sig({ id: "e1", kind: "StructuredOutput", payload: { data: { gate_verdict: "reject" } } }),
      sig({ id: "e2", kind: "StructuredOutput", payload: { data: { gate_verdict: "approve" } } }),
    ]);
    expect(outcome?.verdict).toBe("approve");
  });

  it("ignores a StructuredOutput signal whose data has neither shape", async () => {
    const { deriveGateOutcome } = await import("./RunDetail");
    const outcome = deriveGateOutcome([
      sig({ kind: "StructuredOutput", payload: { data: { assignments: [] } } }),
    ]);
    expect(outcome).toBeNull();
  });

  it("does not badge a coding-engine result shape (bare `passed`, no gate key)", async () => {
    const { deriveGateOutcome } = await import("./RunDetail");
    const outcome = deriveGateOutcome([
      sig({
        kind: "StructuredOutput",
        payload: {
          data: {
            passed: true,
            measurements: { rounds: 2 },
            caveats: [],
            experiment_ref: "",
            verdict_ref: "V1",
          },
        },
      }),
    ]);
    expect(outcome).toBeNull();
  });

  it("does not badge a hypothesis-engine result shape (bare `passed`, no gate key)", async () => {
    const { deriveGateOutcome } = await import("./RunDetail");
    const outcome = deriveGateOutcome([
      sig({
        kind: "StructuredOutput",
        payload: {
          data: {
            passed: false,
            measurements: "0/3 assertions held",
            caveats: ["budget exhausted"],
            experiment_ref: "E1",
          },
        },
      }),
    ]);
    expect(outcome).toBeNull();
  });

  it("does not badge a generic Verdict/ComplianceVerdict shape (bare `verdict`, no gate_verdict key)", async () => {
    const { deriveGateOutcome } = await import("./RunDetail");
    const outcome = deriveGateOutcome([
      sig({
        kind: "StructuredOutput",
        payload: {
          data: { verdict: "REJECT", rationale: "unmet acceptance criteria", unmet: ["a"] },
        },
      }),
    ]);
    expect(outcome).toBeNull();
  });

  it("does not badge a hypothesis-engine ConclusionDrawn shape (bare `verdict`, no gate_verdict key)", async () => {
    const { deriveGateOutcome } = await import("./RunDetail");
    const outcome = deriveGateOutcome([
      sig({
        kind: "StructuredOutput",
        payload: {
          data: {
            verdict: "confirmed",
            rationale: "3/3 assertions held",
            question_ref: "Q1",
            result_ref: "R1",
            basis: "empirical",
            confidence: 0.8,
            limitations: [],
          },
        },
      }),
    ]);
    expect(outcome).toBeNull();
  });

  // A flow-layer DAG gate (lionagi/operations/flow.py's is_gate contract) never
  // emits a StructuredOutput signal — its rejection surfaces only as this
  // session-level terminal reason code (lionagi/cli/_runs.py, RunReasons.
  // COMPLETED_GATE_REJECTED). deriveGateOutcome must read that shape too, or a
  // DAG gate can reject with no badge ever appearing.
  it("badges a reject from the session's gate-rejected reason code when no StructuredOutput verdict exists", async () => {
    const { deriveGateOutcome } = await import("./RunDetail");
    const outcome = deriveGateOutcome(
      [sig({ kind: "NodeCompleted", payload: { name: "step1" } })],
      { status_reason_code: "run.completed.gate_rejected" },
    );
    expect(outcome).toEqual({ verdict: "reject", major: 0, minor: 0, hasFindings: false });
  });

  it("does not badge on an unrelated terminal reason code", async () => {
    const { deriveGateOutcome } = await import("./RunDetail");
    const outcome = deriveGateOutcome(
      [sig({ kind: "NodeCompleted", payload: { name: "step1" } })],
      { status_reason_code: "run.completed.ok" },
    );
    expect(outcome).toBeNull();
  });

  it("prefers a StructuredOutput verdict over the gate-rejected reason code", async () => {
    const { deriveGateOutcome } = await import("./RunDetail");
    const outcome = deriveGateOutcome(
      [sig({ kind: "StructuredOutput", payload: { data: { gate_verdict: "approve" } } })],
      { status_reason_code: "run.completed.gate_rejected" },
    );
    expect(outcome?.verdict).toBe("approve");
  });
});

// ─── EventsSection — "show older" paging ─────────────────────────────────────
// The events list renders only the newest `renderStep` rows and pages older
// rows in on click; a bug here would either drop rows or scramble the
// chronological order readers rely on when scanning a run's history.

describe("history/RunDetail.tsx — EventsSection show-older paging", () => {
  function hookEvents(count: number) {
    return Array.from({ length: count }, (_, i) =>
      sig({ id: `e${i}`, kind: "HookSignal", payload: { point: `p${i}` } }),
    );
  }

  function renderEvents(events: ReturnType<typeof hookEvents>, renderStep: number) {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    mountedCards.push({ container, root });
    act(() => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <EventsSectionForTest events={events} live={false} renderStep={renderStep} />
        </IntlProvider>,
      );
    });
    return container;
  }

  function visiblePoints(container: HTMLDivElement) {
    return Array.from(container.querySelectorAll("#run-events .divide-y > div")).map((row) => {
      const match = row.textContent?.match(/p(\d+)/);
      return match ? `p${match[1]}` : null;
    });
  }

  let EventsSectionForTest: (typeof import("./RunDetail"))["EventsSection"];

  beforeAll(async () => {
    ({ EventsSection: EventsSectionForTest } = await import("./RunDetail"));
  });

  it("clicking 'show older' pages back further while preserving chronological order", () => {
    const events = hookEvents(7); // p0..p6
    const container = renderEvents(events, 3);

    // Only the newest 3 rows render initially, oldest-to-newest within the window.
    expect(visiblePoints(container)).toEqual(["p4", "p5", "p6"]);

    const button = container.querySelector("button");
    expect(button).not.toBeNull();
    act(() => {
      button?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    // Paging back reveals the next-older 3 rows, prepended in order — the
    // previously-visible rows keep their relative order, nothing is reshuffled.
    expect(visiblePoints(container)).toEqual(["p1", "p2", "p3", "p4", "p5", "p6"]);
  });
});

describe("stale-write guard predicate (mirrors the done handler's merge condition)", () => {
  function mergeIfSameSession(
    prev: { id: string; status: string } | null,
    fresh: { id: string; status: string },
  ): { id: string; status: string } | null {
    if (!prev || prev.id !== fresh.id) return prev;
    return { ...prev, status: fresh.status };
  }

  it("merges when the fresh fetch matches the currently-viewed session", () => {
    const prev = { id: "run-a", status: "running" };
    const result = mergeIfSameSession(prev, { id: "run-a", status: "completed" });
    expect(result?.status).toBe("completed");
  });

  it("drops a stale fetch for a session the viewer has since navigated away from", () => {
    const prev = { id: "run-b", status: "running" };
    const result = mergeIfSameSession(prev, { id: "run-a", status: "completed" });
    expect(result?.id).toBe("run-b");
    expect(result?.status).toBe("running");
  });

  it("no-ops when there is no current session", () => {
    expect(mergeIfSameSession(null, { id: "run-a", status: "completed" })).toBeNull();
  });
});

// ─── resolveGraphEdges — planner step numbers become node ids ────────────────
// The planner persists depends_on endpoints as 1-based step numbers ("1")
// while the graph's nodes are keyed by role name ("explorer"). Passed through
// unresolved, every edge dangles: dagre invents phantom zero-size nodes and
// the layout shatters into disconnected clusters (measured 125/125 edges
// unresolvable on a live 30-node run). resolveGraphEdges maps numeric refs
// onto the node at that position and drops what it cannot resolve.

describe("history/RunDetail.tsx — resolveGraphEdges", () => {
  const graphNodes = (...ids: string[]) =>
    ids.map((id) => ({ id })) as unknown as import("@/lib/types").WorkerGraph["nodes"];
  const edge = (id: string, source: string, target: string) =>
    ({ id, source, target, mode: "simple" }) as const;

  it("resolves 1-based numeric refs to the node at that position", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    const nodes = graphNodes("explorer", "critic", "synth");
    const out = resolveGraphEdges(nodes, [edge("e1", "1", "2"), edge("e2", "2", "3")]);
    expect(out).toEqual([
      { id: "e1", source: "explorer", target: "critic", mode: "simple" },
      { id: "e2", source: "critic", target: "synth", mode: "simple" },
    ]);
  });

  it("keeps refs that already match node ids, mixed with numeric refs", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    const nodes = graphNodes("explorer", "critic");
    const out = resolveGraphEdges(nodes, [edge("e1", "explorer", "2")]);
    expect(out).toEqual([{ id: "e1", source: "explorer", target: "critic", mode: "simple" }]);
  });

  it("prefers an exact id match over positional reading for a numeric node id", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    // A node literally named "2": the ref must mean THAT node, not position 2.
    const nodes = graphNodes("2", "critic");
    const out = resolveGraphEdges(nodes, [edge("e1", "2", "critic")]);
    expect(out).toEqual([{ id: "e1", source: "2", target: "critic", mode: "simple" }]);
  });

  it("drops edges whose endpoints resolve to nothing", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    const nodes = graphNodes("explorer", "critic");
    const out = resolveGraphEdges(nodes, [
      edge("e1", "99", "critic"), // position out of range
      edge("e2", "phantom", "critic"), // unknown name
      edge("e3", "1", "2"), // resolvable — must survive the same pass
    ]);
    expect(out).toEqual([{ id: "e3", source: "explorer", target: "critic", mode: "simple" }]);
  });

  it("drops an edge whose endpoints resolve to the same node", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    const nodes = graphNodes("explorer", "critic");
    // "1" and "explorer" are the same node spelled two ways.
    const out = resolveGraphEdges(nodes, [edge("e1", "1", "explorer")]);
    expect(out).toEqual([]);
  });

  it("returns [] for null, undefined, or empty edges", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    const nodes = graphNodes("explorer");
    expect(resolveGraphEdges(nodes, null)).toEqual([]);
    expect(resolveGraphEdges(nodes, undefined)).toEqual([]);
    expect(resolveGraphEdges(nodes, [])).toEqual([]);
  });

  it("preserves the edge's other properties through resolution", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    const nodes = graphNodes("explorer", "critic");
    const conditional = { id: "e1", source: "1", target: "2", condition: "score > 0.8" };
    const out = resolveGraphEdges(nodes, [
      conditional,
    ] as unknown as import("@/lib/types").WorkerGraph["edges"]);
    expect(out[0]).toMatchObject({
      source: "explorer",
      target: "critic",
      condition: "score > 0.8",
    });
  });
});

describe("history/RunDetail.tsx — resolveGraphEdges dedupes what resolution collapses", () => {
  const graphNodes = (...ids: string[]) =>
    ids.map((id) => ({ id })) as unknown as import("@/lib/types").WorkerGraph["nodes"];
  const edge = (id: string, source: string, target: string) =>
    ({ id, source, target, mode: "simple" }) as const;

  it("drops the second edge when a numeric ref and the id it names arrive as two edges", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    const nodes = graphNodes("explorer", "critic");
    // "1"→"2" and "explorer"→"critic" are one dependency spelled two ways.
    const out = resolveGraphEdges(nodes, [edge("e1", "1", "2"), edge("e2", "explorer", "critic")]);
    expect(out).toEqual([{ id: "e1", source: "explorer", target: "critic", mode: "simple" }]);
  });

  it("drops a repeated edge id even when the pairs differ", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    const nodes = graphNodes("explorer", "critic", "synth");
    const out = resolveGraphEdges(nodes, [
      edge("dup", "explorer", "critic"),
      edge("dup", "explorer", "synth"),
    ]);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ source: "explorer", target: "critic" });
  });

  it("keeps distinct edges between distinct pairs untouched", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    const nodes = graphNodes("explorer", "critic", "synth");
    const out = resolveGraphEdges(nodes, [
      edge("e1", "explorer", "critic"),
      edge("e2", "critic", "synth"),
      edge("e3", "explorer", "synth"),
    ]);
    expect(out).toHaveLength(3);
  });
});

describe("history/RunDetail.tsx — a collapsed pair keeps its richer edge", () => {
  const graphNodes = (...ids: string[]) =>
    ids.map((id) => ({ id })) as unknown as import("@/lib/types").WorkerGraph["nodes"];

  it("a condition-bearing edge survives a bare duplicate that arrived FIRST", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    const nodes = graphNodes("explorer", "critic");
    const out = resolveGraphEdges(nodes, [
      { id: "bare", source: "1", target: "2", mode: "simple" },
      {
        id: "cond",
        source: "explorer",
        target: "critic",
        mode: "simple",
        condition: "score > 0.8",
      },
    ]);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ id: "cond", condition: "score > 0.8" });
  });

  it("a condition-bearing edge survives a bare duplicate that arrived SECOND", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    const nodes = graphNodes("explorer", "critic");
    const out = resolveGraphEdges(nodes, [
      {
        id: "cond",
        source: "explorer",
        target: "critic",
        mode: "simple",
        condition: "score > 0.8",
      },
      { id: "bare", source: "1", target: "2", mode: "simple" },
    ]);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ id: "cond", condition: "score > 0.8" });
  });

  it("a replaced pair keeps its original position in the edge order", async () => {
    const { resolveGraphEdges } = await import("./RunDetail");
    const nodes = graphNodes("explorer", "critic", "synth");
    const out = resolveGraphEdges(nodes, [
      { id: "bare", source: "1", target: "2", mode: "simple" },
      { id: "other", source: "critic", target: "synth", mode: "simple" },
      { id: "cond", source: "explorer", target: "critic", mode: "simple", condition: "x" },
    ]);
    expect(out.map((e) => e.id)).toEqual(["cond", "other"]);
  });
});

// ─── Graph-node drill-down: matching ───────────────────────────────────────
// Graph nodes are keyed by authored role/assignment name (WorkerStepNode.id);
// branches carry agent_name, falling back to name, then an id prefix — see
// implementation_brief.md and the measured RunDetail.tsx:335 formula. Both
// match arms (a node WITH a branch, a node WITHOUT one) are exercised here.

function makeBranch(overrides: Partial<import("@/lib/api").SessionBranch>) {
  return {
    id: "abcdef1234567890",
    name: "",
    created_at: 0,
    messages: [],
    ...overrides,
  } as import("@/lib/api").SessionBranch;
}

describe("history/RunDetail.tsx — matchGraphNodeToBranch (graph-node drill-down)", () => {
  it("match arm: resolves by exact branch name first, ahead of agent_name", async () => {
    // branch.name is unique/durable per session; agent_name is a role label
    // shared by every branch with that role. An exact name match must win
    // even when a different branch's agent_name also matches the node id.
    const { matchGraphNodeToBranch } = await import("./RunDetail");
    const branches = [
      makeBranch({ id: "b1", name: "analyst-role", agent_name: "analyst" }),
      makeBranch({ id: "b2", name: "analyst", agent_name: null }),
    ];
    const match = matchGraphNodeToBranch("analyst", branches);
    expect(match?.id).toBe("b2");
  });

  it("match arm: falls back to agent_name only when exactly one branch carries it", async () => {
    const { matchGraphNodeToBranch } = await import("./RunDetail");
    const branches = [makeBranch({ id: "b1", name: "analyst-role", agent_name: "analyst" })];
    const match = matchGraphNodeToBranch("analyst", branches);
    expect(match?.id).toBe("b1");
  });

  it("match arm: two branches sharing a role's agent_name is ambiguous — resolves via the unique branch name instead, regardless of list order", async () => {
    // Duplicate-implementer scenario: {name:"implementer-2",
    // agent_name:"implementer"} ordered before the branch whose exact name
    // is the clicked node id. agent_name alone can't disambiguate (both
    // branches carry it) — the exact name match must win, and win the same
    // way whichever order the branches list arrives in.
    const { matchGraphNodeToBranch } = await import("./RunDetail");
    const forward = [
      makeBranch({ id: "b1", name: "implementer-2", agent_name: "implementer" }),
      makeBranch({ id: "b2", name: "implementer", agent_name: "implementer" }),
    ];
    expect(matchGraphNodeToBranch("implementer", forward)?.id).toBe("b2");

    const reversed = [
      makeBranch({ id: "b2", name: "implementer", agent_name: "implementer" }),
      makeBranch({ id: "b1", name: "implementer-2", agent_name: "implementer" }),
    ];
    expect(matchGraphNodeToBranch("implementer", reversed)?.id).toBe("b2");
  });

  it("match arm: falls back to name when no agent_name matches", async () => {
    const { matchGraphNodeToBranch } = await import("./RunDetail");
    const branches = [
      makeBranch({ id: "b1", name: "other", agent_name: "someone-else" }),
      makeBranch({ id: "b2", name: "tester", agent_name: null }),
    ];
    const match = matchGraphNodeToBranch("tester", branches);
    expect(match?.id).toBe("b2");
  });

  it("match arm: falls back to an 8-char id prefix when neither agent_name nor name matches", async () => {
    const { matchGraphNodeToBranch } = await import("./RunDetail");
    const branches = [makeBranch({ id: "9e5f593fabcdef01", name: "", agent_name: null })];
    const match = matchGraphNodeToBranch("9e5f593f", branches);
    expect(match?.id).toBe("9e5f593fabcdef01");
  });

  it("unmatched arm: returns null when nothing resolves — the explicit no-branch case", async () => {
    const { matchGraphNodeToBranch } = await import("./RunDetail");
    const branches = [makeBranch({ id: "b1", name: "tester", agent_name: "tester" })];
    expect(matchGraphNodeToBranch("nonexistent-role", branches)).toBeNull();
  });

  it("matched branch resolves to the SAME key branchToRunStep uses (stepKeyForBranch identity)", async () => {
    const { matchGraphNodeToBranch, stepKeyForBranch, branchToRunStep } =
      await import("./RunDetail");
    const branch = makeBranch({ id: "b1", name: "reviewer", agent_name: "reviewer" });
    const match = matchGraphNodeToBranch("reviewer", [branch]);
    expect(match).not.toBeNull();
    const key = stepKeyForBranch(match!);
    const step = branchToRunStep(branch, "completed");
    // The drill-down expands/highlights expandedSteps.has(key) and scrolls to
    // `#step-${key}` — both must agree with what RunStepCard actually renders.
    expect(key).toBe(step.step);
  });
});

// ─── Header-source identity + terminal no-signal presentation ─────────────
// The progress summary and the graph nodes must derive from the exact same
// reconciled status map, and a node with no lifecycle signal on a finished
// run must never present as "running".

describe("history/RunDetail.tsx — computeReconciledNodeStatuses / computeProgressCountsForGraph", () => {
  const graph = {
    nodes: [
      { id: "a" },
      { id: "b" },
      { id: "c" },
    ] as unknown as import("@/lib/types").WorkerGraph["nodes"],
    edges: [{ source: "a", target: "b" }] as unknown as import("@/lib/types").WorkerGraph["edges"],
  };

  it("terminal no-signal presentation: an isolated node stuck 'running' on a DONE run reads pending, never running", async () => {
    const { computeReconciledNodeStatuses } = await import("./RunDetail");
    // "c" has no outgoing edges (no descendant to trigger suppression) and no
    // terminal signal was ever recorded for it — on a done run that must
    // read as absence of information ("pending"), never as live work.
    const reconciled = computeReconciledNodeStatuses(graph, { c: "running" }, true);
    expect(reconciled?.c).toBe("pending");
  });

  it("descendant-terminal suppression corrects a stale 'running' reading to 'completed' before the terminal-run collapse runs", async () => {
    const { computeReconciledNodeStatuses } = await import("./RunDetail");
    // "a" still reads "running" but its descendant "b" already completed —
    // "a" could not still be running, so it resolves to "completed" (a
    // terminal status), not "pending".
    const reconciled = computeReconciledNodeStatuses(graph, { a: "running", b: "completed" }, true);
    expect(reconciled?.a).toBe("completed");
  });

  it("descendant-terminal suppression holds even on a still-live run (not done-gated)", async () => {
    const { computeReconciledNodeStatuses } = await import("./RunDetail");
    const reconciled = computeReconciledNodeStatuses(
      graph,
      { a: "running", b: "completed" },
      false,
    );
    expect(reconciled?.a).toBe("completed");
  });

  it("header-source identity: counts are derived from the exact reconciled map, so they cannot diverge from what the graph would render", async () => {
    const { computeReconciledNodeStatuses, computeProgressCountsForGraph } =
      await import("./RunDetail");
    const reconciled = computeReconciledNodeStatuses(graph, { a: "running", b: "completed" }, true);
    const counts = computeProgressCountsForGraph(graph, reconciled);
    // Same map both consumers would read: a→completed (descendant
    // suppression, since b already completed), b→completed, c→pending (no
    // entry, default — collapse leaves it as-is since it was never active).
    expect(counts).toMatchObject({ total: 3, completed: 2, running: 0, pending: 1, failed: 0 });
    expect(counts?.hasFailure).toBe(false);
  });

  it("counts escalated separately while genuine failed still trips the failure header", async () => {
    const { computeReconciledNodeStatuses, computeProgressCountsForGraph } =
      await import("./RunDetail");
    const reconciled = computeReconciledNodeStatuses(graph, { a: "escalated", b: "failed" }, true);
    const pairedCounts = computeProgressCountsForGraph(graph, reconciled);
    expect(pairedCounts).toMatchObject({ escalated: 1, failed: 1, hasFailure: true });

    const escalatedOnly = computeProgressCountsForGraph(
      { nodes: graph.nodes.slice(0, 1) },
      { a: "escalated" },
    );
    expect(escalatedOnly).toMatchObject({ escalated: 1, failed: 0, hasFailure: false });
  });

  it("returns undefined/null gracefully when there is no run graph yet", async () => {
    const { computeReconciledNodeStatuses, computeProgressCountsForGraph } =
      await import("./RunDetail");
    expect(computeReconciledNodeStatuses(null, undefined, false)).toBeUndefined();
    expect(computeProgressCountsForGraph(null, undefined)).toBeNull();
  });
});

// ─── Expand / close wiring + full-content-width placement ─────────────────
// Source-text checks mirroring the existing wiring-assertion style in this
// file (e.g. "authored run graph is rendered unreduced" above) — the
// behavior itself (open/close/Escape) is a DOM-event state machine that is
// exercised end-to-end by the pure reducer style tests above and by manual
// verification (documented in run_detail_implementation.md); these pin the
// wiring so a refactor can't silently drop the close paths.

describe("history/RunDetail.tsx — execution-graph expand/close wiring", () => {
  const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");

  it("Escape closes the expanded graph overlay", () => {
    expect(src).toMatch(/event\.key === "Escape"/);
    expect(src).toMatch(/setGraphExpanded\(false\)/);
  });

  it("an explicit close button also closes the overlay", () => {
    expect(src).toMatch(/onClick={\(\) => setGraphExpanded\(false\)}/);
  });

  it("the expand control opens the overlay", () => {
    expect(src).toMatch(/onClick={\(\) => setGraphExpanded\(true\)}/);
  });

  it("the run-dag panel is not constrained narrower than its flex parent (full-content-width placement)", () => {
    expect(src).toMatch(/id="run-dag" className="w-full scroll-mt-4"/);
  });

  it("both the inline and expanded WorkerCanvas embeds read nodeStatuses from the same reconciled map", () => {
    const occurrences = src.match(/nodeStatuses={reconciledNodeStatuses}/g) ?? [];
    expect(occurrences.length).toBe(2);
    // No remaining callsite passes the raw (unreconciled) map to the graph.
    expect(src).not.toMatch(/nodeStatuses={nodeStatuses}/);
  });

  it("the progress summary bar renders from the same progressCounts used by both graph embeds", () => {
    const occurrences = src.match(/<ProgressSummaryBar counts={progressCounts}/g) ?? [];
    expect(occurrences.length).toBe(2);
  });
});

// ─── Unmatched-node explicit state ─────────────────────────────────────────

describe("history/RunDetail.tsx — unmatched graph-node click shows an explicit state", () => {
  const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");

  it("a click that resolves no branch sets unmatchedNodeId instead of silently no-opping", () => {
    expect(src).toMatch(/setUnmatchedNodeId\(nodeId\)/);
  });

  it("renders the explicit no-branch state when unmatchedNodeId is set", () => {
    expect(src).toMatch(/data-testid="run-dag-unmatched-node"/);
    expect(src).toMatch(/t\("nodeNoBranch", \{ node: unmatchedNodeId \}\)/);
  });

  it("a subsequent matched click clears the no-branch state", () => {
    expect(src).toMatch(/setUnmatchedNodeId\(null\)/);
  });
});

// ─── Follow-mode wiring (live/done) into WorkerCanvas ──────────────────────
//
// WorkerCanvas's follow-mode reducer (initialFollowModeState(live, done)) and
// its "Follow"/"Following" toggle (gated on `live`, see WorkerCanvas.tsx) are
// dead in production unless RunDetail actually passes its own `live`/`done`
// state down as props — both default to `false` in WorkerCanvas, so an
// embed that omits them behaves as an already-finished, never-live run no
// matter what the session is actually doing. RunDetail already computes
// `live`/`done` (used a few lines below for `OperationGraphSection`), so this
// pins that the SAME values reach every WorkerCanvas embed too.

describe("history/RunDetail.tsx — WorkerCanvas live/done wiring for follow-mode", () => {
  const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");
  const workerCanvasBlocks = src.match(/<WorkerCanvas[^]*?\/>/g) ?? [];

  it("finds both the inline and expanded WorkerCanvas embeds to check", () => {
    expect(workerCanvasBlocks.length).toBe(2);
  });

  it("every WorkerCanvas embed passes the run's live state, so follow-mode can activate on a live run", () => {
    for (const block of workerCanvasBlocks) {
      expect(block).toMatch(/\blive={/);
    }
  });

  it("every WorkerCanvas embed passes the run's done state, so follow-mode is force-disabled on a finished run", () => {
    for (const block of workerCanvasBlocks) {
      expect(block).toMatch(/\bdone={/);
    }
  });
});

// ─── Expanded-overlay status persistence (onLayoutHeight stability) ───────
//
// WorkerCanvas's layout effect lists `onLayoutHeight` in its dependency
// array (see WorkerCanvas.tsx), so a fresh inline arrow passed on every
// RunDetail rerender re-triggers a bare relayout that clears execStatus
// until the separate status-application effect happens to also rerun. An
// inline `() => {}` at the expanded call site reproduced exactly this: the
// expanded graph flashed to all-pending while the inline panel kept
// completed/running styling. Both embeds must reference a stable
// (useCallback/useRef-backed) identifier, never an inline arrow.

describe("history/RunDetail.tsx — WorkerCanvas onLayoutHeight is a stable reference", () => {
  const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");
  const workerCanvasBlocks = src.match(/<WorkerCanvas[^]*?\/>/g) ?? [];

  it("finds both WorkerCanvas embeds", () => {
    expect(workerCanvasBlocks.length).toBe(2);
  });

  it("no embed passes an inline arrow function as onLayoutHeight — that identity churns every render and re-triggers WorkerCanvas's layout effect, clobbering execStatus", () => {
    for (const block of workerCanvasBlocks) {
      const match = block.match(/onLayoutHeight={([^}]*)}/);
      expect(match).not.toBeNull();
      expect(match![1]).not.toMatch(/=>/);
    }
  });

  it("the expanded embed's onLayoutHeight identifier is declared via useCallback so it is stable across rerenders", () => {
    const expandedBlockIndex = src.indexOf("closeExpandedGraph");
    const expandedWorkerCanvas = workerCanvasBlocks.find(
      (b) => src.indexOf(b) > expandedBlockIndex,
    );
    expect(expandedWorkerCanvas).toBeDefined();
    const match = expandedWorkerCanvas!.match(/onLayoutHeight={(\w+)}/);
    expect(match).not.toBeNull();
    const identifier = match![1];
    expect(src).toMatch(new RegExp(`const ${identifier} = useCallback\\(`));
  });
});

// ─── Dag panel height policy (floor / grow-only) ────────────────────────────
//
// computeReservedHeight (useLayout.ts) reports the EXACT height a graph will
// render at its applied zoom, and that helper is unit-tested in isolation.
// But the production panel (dagHeight, driven by onDagLayoutHeight below)
// intentionally does not always reserve that exact number: it floors to
// DAG_MIN_HEIGHT, with no ceiling (a capped card would force fitView below
// the readability floor for a graph taller than the cap — the enclosing page
// scrolls past a tall card instead), and — for a given run id — only ever
// grows, never shrinks, so a mid-stream layout that computes a smaller
// height than what's already committed does not shrink the panel underneath
// the reader. This test pins that policy directly against the real
// onDagLayoutHeight reducer logic (mirrored here byte-for-byte from source,
// since the closure isn't exported), not just against computeReservedHeight.

describe("history/RunDetail.tsx — the dag panel height policy is floor/grow-only", () => {
  const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");
  const DAG_MIN_HEIGHT = 280;

  it("pins the floor constant the policy tests below assume", () => {
    expect(src).toMatch(/const DAG_MIN_HEIGHT = 280;/);
  });

  it("onDagLayoutHeight floors the incoming computeReservedHeight value, then only grows the committed height for the run id, with no ceiling", () => {
    expect(src).toMatch(/const clamped = Math\.max\(DAG_MIN_HEIGHT, Math\.ceil\(height\)\);/);
    expect(src).toMatch(
      /height: Math\.max\(prev\.id === id \? prev\.height : DAG_MIN_HEIGHT, clamped\),/,
    );
  });

  // Reference implementation matching the source above, so the *behavior* —
  // not just the presence of the lines — is pinned.
  function reduce(
    prev: { id: string; height: number },
    id: string,
    height: number,
  ): { id: string; height: number } {
    const clamped = Math.max(DAG_MIN_HEIGHT, Math.ceil(height));
    return { id, height: Math.max(prev.id === id ? prev.height : DAG_MIN_HEIGHT, clamped) };
  }

  it("a layout below the floor is floored, not passed through", () => {
    const result = reduce({ id: "run-1", height: DAG_MIN_HEIGHT }, "run-1", 120);
    expect(result.height).toBe(DAG_MIN_HEIGHT);
  });

  it("a layout far above the floor is passed through — no ceiling", () => {
    const result = reduce({ id: "run-1", height: DAG_MIN_HEIGHT }, "run-1", 4000);
    expect(result.height).toBe(4000);
  });

  it("a later smaller layout for the SAME run never shrinks the committed height (grow-only mid-stream)", () => {
    const grown = reduce({ id: "run-1", height: DAG_MIN_HEIGHT }, "run-1", 420);
    expect(grown.height).toBe(420);
    const shrunk = reduce(grown, "run-1", 300);
    expect(shrunk.height).toBe(420);
  });

  it("switching to a DIFFERENT run id resets the floor instead of carrying over the previous run's committed height", () => {
    const grown = reduce({ id: "run-1", height: DAG_MIN_HEIGHT }, "run-1", 420);
    const nextRun = reduce(grown, "run-2", 150);
    expect(nextRun.height).toBe(DAG_MIN_HEIGHT);
  });
});

// ─── ADR-0113 rows 3 & 5: graph/list view — pure resolution logic ──────────

describe("history/RunDetail.tsx — hasResolvableGraph (row 3: what counts as a real canvas)", () => {
  const node = (id: string) => ({
    id,
    label: id,
    role: "",
    assignment: "",
    prompt: "",
    capacity: 1,
    timeout: null,
    inputs: [],
    outputs: [],
  });

  it("a graph with edges is resolvable", async () => {
    const { hasResolvableGraph } = await import("./RunDetail");
    const graph = {
      nodes: [node("A"), node("B")],
      edges: [{ id: "e1", source: "A", target: "B", mode: "simple" as const }],
    };
    expect(hasResolvableGraph(graph, { nodes: [], edges: [] })).toBe(true);
  });

  it("a single node with no edges is NOT resolvable — a canvas with one node and no edges is not a canvas", async () => {
    const { hasResolvableGraph } = await import("./RunDetail");
    const graph = { nodes: [node("A")], edges: [] };
    expect(hasResolvableGraph(graph, { nodes: [], edges: [] })).toBe(false);
  });

  it("several disconnected nodes with no edges are also not resolvable — same reasoning, not just the one-node case", async () => {
    const { hasResolvableGraph } = await import("./RunDetail");
    const graph = { nodes: [node("A"), node("B"), node("C")], edges: [] };
    expect(hasResolvableGraph(graph, { nodes: [], edges: [] })).toBe(false);
  });

  it("falls through to a real-edged opGraph when the authored graph is edgeless (mirrors shouldRenderAuthoredGraph)", async () => {
    const { hasResolvableGraph } = await import("./RunDetail");
    const authoredEdgeless = { nodes: [node("A"), node("B")], edges: [] };
    const opGraph = {
      nodes: [{ id: "A" }, { id: "B" }],
      edges: [{ id: "e1", source: "A", target: "B" }],
    };
    expect(hasResolvableGraph(authoredEdgeless, opGraph)).toBe(true);
  });

  // The one-node case takes the same fall-through. It used to be excluded,
  // which meant an authored snapshot of a single node hid a runtime graph
  // that had two nodes and an edge between them: canRenderGraph came back
  // false and the run opened on the list with a real DAG sitting unrendered.
  it("falls through to a real-edged opGraph even when the authored graph has exactly one node", async () => {
    const { hasResolvableGraph } = await import("./RunDetail");
    const authoredSingleNode = { nodes: [node("A")], edges: [] };
    const opGraph = {
      nodes: [{ id: "A" }, { id: "B" }],
      edges: [{ id: "e1", source: "A", target: "B" }],
    };
    expect(hasResolvableGraph(authoredSingleNode, opGraph)).toBe(true);
  });

  it("no graph at all is not resolvable", async () => {
    const { hasResolvableGraph } = await import("./RunDetail");
    expect(hasResolvableGraph(null, { nodes: [], edges: [] })).toBe(false);
  });
});

describe("history/RunDetail.tsx — resolveInitialView (row 5: the precedence rule)", () => {
  it("defaults to graph when a resolvable graph exists and the reader has made no choice", async () => {
    const { resolveInitialView } = await import("./RunDetail");
    expect(
      resolveInitialView({ urlView: null, storedPreference: null, hasResolvableGraph: true }),
    ).toBe("graph");
  });

  it("defaults to list when there is no resolvable graph and the reader has made no choice", async () => {
    const { resolveInitialView } = await import("./RunDetail");
    expect(
      resolveInitialView({ urlView: null, storedPreference: null, hasResolvableGraph: false }),
    ).toBe("list");
  });

  // This is the precedence the brief calls out explicitly: "default is
  // graph" and "preference is persisted" are in tension exactly once — a
  // graph-having run whose reader has already chosen "list" — and the
  // reader's choice must win. A regression that makes the default win
  // again (e.g. checking hasResolvableGraph before storedPreference) fails
  // this test.
  it("a stored 'list' preference beats the graph default, even when a resolvable graph exists", () => {
    return import("./RunDetail").then(({ resolveInitialView }) => {
      expect(
        resolveInitialView({
          urlView: null,
          storedPreference: "list",
          hasResolvableGraph: true,
        }),
      ).toBe("list");
    });
  });

  it("the URL view param outranks the stored preference (a pasted deep link reproduces what was shared)", async () => {
    const { resolveInitialView } = await import("./RunDetail");
    expect(
      resolveInitialView({
        urlView: "graph",
        storedPreference: "list",
        hasResolvableGraph: false,
      }),
    ).toBe("graph");
  });
});

// ─── ADR-0113 rows 3, 5, 8, 9 — mounted behavior ────────────────────────────
//
// Reuses the mount pattern from the hidden-count-badge suite above
// (getSession/streamSession/streamSignals/ResumeRun/WorkerCanvas mocked at
// module scope): real RunDetail, real BranchesSection/RunStepCard, a fake
// WorkerCanvas standing in for ReactFlow.

describe("history/RunDetail.tsx — graph/list view toggle and cross-view selection, mounted", () => {
  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    Element.prototype.scrollIntoView = vi.fn();
    // localStorage is unavailable in this test runtime (jsdom under Node's
    // experimental webstorage without a backing file) — RunDetail's own
    // reads/writes already tolerate that (readStoredView/writeStoredView),
    // so these tests exercise the URL query param instead, which jsdom does
    // support. The stored-preference SIDE of the precedence rule is covered
    // directly against resolveInitialView (pure function, above), which
    // needs no browser storage at all.
    window.history.replaceState(null, "", "/");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const node = (id: string) => ({
    id,
    label: id,
    role: "",
    assignment: "",
    prompt: "",
    capacity: 1,
    timeout: null,
    inputs: [],
    outputs: [],
  });

  const edgedGraph = {
    name: "run",
    description: "",
    nodes: [node("A"), node("B")],
    edges: [{ id: "e-ab", source: "A", target: "B", mode: "simple" as const }],
  };

  const singleNodeGraph = {
    name: "run",
    description: "",
    nodes: [node("A")],
    edges: [],
  };

  function sessionWithBranches(graph: unknown, invocationKind: string | null = null) {
    return {
      id: "run-mount-view",
      name: "run-mount-view",
      created_at: 0,
      updated_at: 0,
      status: "completed",
      invocation_kind: invocationKind,
      branches: [{ id: "branch-a", name: "A", created_at: 0, messages: [] }],
      graph,
    };
  }

  async function mountRunDetail(session: unknown) {
    const [{ getSession }, { default: RunDetail }] = await Promise.all([
      import("@/lib/api"),
      import("./RunDetail"),
    ]);
    vi.mocked(getSession).mockResolvedValue(session as never);

    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <RunDetail id="run-mount-view" />
        </IntlProvider>,
      );
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    return {
      container,
      unmount: () => {
        act(() => root.unmount());
        container.remove();
      },
    };
  }

  it("row 3: a run with edges opens on the graph", async () => {
    const { container, unmount } = await mountRunDetail(sessionWithBranches(edgedGraph));
    try {
      expect(container.querySelector('[data-testid="worker-canvas"]')).not.toBeNull();
      expect(container.querySelector("#run-branches")).toBeNull();
      const graphTab = container.querySelector('[data-testid="run-detail-view-graph"]');
      expect(graphTab?.getAttribute("aria-selected")).toBe("true");
    } finally {
      unmount();
    }
  });

  it("row 3: a single-node run with no edges opens on the list, and offers no graph tab to switch to — a graph with no edges is not a canvas worth exposing at all", async () => {
    const { container, unmount } = await mountRunDetail(sessionWithBranches(singleNodeGraph));
    try {
      expect(container.querySelector('[data-testid="worker-canvas"]')).toBeNull();
      expect(container.querySelector("#run-branches")).not.toBeNull();
      // No view toggle at all — there is nothing to switch to. Before the
      // resolved-graph consolidation, canRenderGraph (looser than
      // hasResolvableGraph) still exposed a clickable Graph tab here, and
      // selecting it rendered a single disconnected node with no edges.
      expect(container.querySelector('[data-testid="run-detail-view-graph"]')).toBeNull();
      expect(container.querySelector('[data-testid="run-detail-view-list"]')).toBeNull();
    } finally {
      unmount();
    }
  });

  it("row 5: an explicit ?view=list in the URL beats the graph default for a run with edges", async () => {
    window.history.replaceState(null, "", "/?view=list");
    const { container, unmount } = await mountRunDetail(sessionWithBranches(edgedGraph));
    try {
      expect(container.querySelector('[data-testid="worker-canvas"]')).toBeNull();
      expect(container.querySelector("#run-branches")).not.toBeNull();
      const listTab = container.querySelector('[data-testid="run-detail-view-list"]');
      expect(listTab?.getAttribute("aria-selected")).toBe("true");
    } finally {
      unmount();
    }
  });

  it("row 5: clicking the List tab switches away from the graph, and Graph switches back", async () => {
    const { container, unmount } = await mountRunDetail(sessionWithBranches(edgedGraph));
    try {
      const listTab = container.querySelector<HTMLButtonElement>(
        '[data-testid="run-detail-view-list"]',
      );
      await act(async () => {
        listTab?.click();
      });
      expect(container.querySelector('[data-testid="worker-canvas"]')).toBeNull();
      expect(container.querySelector("#run-branches")).not.toBeNull();

      const graphTab = container.querySelector<HTMLButtonElement>(
        '[data-testid="run-detail-view-graph"]',
      );
      await act(async () => {
        graphTab?.click();
      });
      expect(container.querySelector('[data-testid="worker-canvas"]')).not.toBeNull();
      expect(container.querySelector("#run-branches")).toBeNull();
    } finally {
      unmount();
    }
  });

  it("row 5: selection survives a view switch — selecting a step in the list, then switching to graph, keeps it as the selected node", async () => {
    const { container, unmount } = await mountRunDetail(sessionWithBranches(edgedGraph));
    try {
      const listTab = container.querySelector<HTMLButtonElement>(
        '[data-testid="run-detail-view-list"]',
      );
      await act(async () => {
        listTab?.click();
      });
      const expandButton = container.querySelector<HTMLButtonElement>(
        'button[aria-controls="step-A-body"]',
      );
      expect(expandButton).not.toBeNull();
      // A session with this few branches auto-expands every step on load
      // (see the session-load effect above), so the single step here starts
      // already expanded — collapsing it first (a plain expand/collapse,
      // not a selection) and then re-expanding it is what actually exercises
      // "the reader opened this step," which is the act that selects it.
      await act(async () => {
        expandButton?.click();
      });
      await act(async () => {
        expandButton?.click();
      });
      const selectedRow = document.getElementById("step-A")?.parentElement;
      expect(selectedRow?.hasAttribute("data-selected")).toBe(true);

      const graphTab = container.querySelector<HTMLButtonElement>(
        '[data-testid="run-detail-view-graph"]',
      );
      await act(async () => {
        graphTab?.click();
      });
      const indicator = container.querySelector('[data-testid="run-detail-selected-node"]');
      expect(indicator?.textContent).toContain("A");
    } finally {
      unmount();
    }
  });

  it("row 3 (runtime path): an opGraph with nodes but no edges falls back to the list, same as an edgeless authored graph", async () => {
    const { streamSignals } = await import("@/lib/api");
    vi.mocked(streamSignals).mockImplementationOnce((_id, cb) => {
      cb(sig({ id: "e1", op_id: "op-a", kind: "NodeStarted", payload: { name: "A" } }));
      return () => {};
    });
    const { container, unmount } = await mountRunDetail(sessionWithBranches(null));
    try {
      expect(container.querySelector('[data-testid="op-graph-node"]')).toBeNull();
      expect(container.querySelector("#run-branches")).not.toBeNull();
      expect(container.querySelector('[data-testid="run-detail-view-graph"]')).toBeNull();
    } finally {
      unmount();
    }
  });

  it("row 4 (authored path): selecting a node renders its detail card in place, in the graph view", async () => {
    const { container, unmount } = await mountRunDetail(sessionWithBranches(edgedGraph));
    try {
      // The mocked WorkerCanvas doesn't render ReactFlow's own node markup —
      // handleDagPanelClick delegates on the raw DOM shape ReactFlow produces
      // (`.react-flow__node[data-id]`), so a synthetic one exercises the same
      // delegation the real graph relies on.
      const canvas = container.querySelector('[data-testid="worker-canvas"]');
      const panel = canvas?.parentElement;
      expect(panel).not.toBeNull();
      const fakeNode = document.createElement("div");
      fakeNode.className = "react-flow__node";
      fakeNode.dataset.id = "A";
      panel!.appendChild(fakeNode);
      await act(async () => {
        fakeNode.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });
      expect(document.getElementById("step-A")).not.toBeNull();
    } finally {
      unmount();
    }
  });

  it("row 4 (runtime path): OperationGraphSection has a node click handler, and selecting a node renders its detail card in place", async () => {
    const { streamSignals } = await import("@/lib/api");
    vi.mocked(streamSignals).mockImplementationOnce((_id, cb) => {
      cb(sig({ id: "e1", op_id: "op-a", kind: "NodeStarted", payload: { name: "A" } }));
      cb(
        sig({
          id: "e2",
          op_id: "op-b",
          kind: "NodeStarted",
          payload: { name: "B", parent_id: "op-a" },
        }),
      );
      return () => {};
    });
    const session = {
      ...sessionWithBranches(null),
      branches: [{ id: "branch-b", name: "B", created_at: 0, messages: [] }],
    };
    const { container, unmount } = await mountRunDetail(session);
    try {
      // An opGraph with a real edge is a resolvable graph, so this defaults
      // to the graph view without needing to click a tab.
      const nodeCard = Array.from(
        container.querySelectorAll<HTMLElement>('[data-testid="op-graph-node"]'),
      ).find((el) => el.textContent?.includes("B"));
      expect(nodeCard).toBeTruthy();
      await act(async () => {
        nodeCard?.click();
      });
      expect(document.getElementById("step-B")).not.toBeNull();
    } finally {
      unmount();
    }
  });

  it("row 6 (D6 URL-addressability): a URL carrying a selected node restores that selection on load", async () => {
    window.history.replaceState(null, "", "/?node=A");
    const { container, unmount } = await mountRunDetail(sessionWithBranches(edgedGraph));
    try {
      expect(document.getElementById("step-A")).not.toBeNull();
      const indicator = container.querySelector('[data-testid="run-detail-selected-node"]');
      expect(indicator?.textContent).toContain("A");
    } finally {
      unmount();
    }
  });

  it("coupled minor: changing the run id clears the previous run's selected node", async () => {
    const [{ getSession }, { default: RunDetail }] = await Promise.all([
      import("@/lib/api"),
      import("./RunDetail"),
    ]);
    const runOne = sessionWithBranches(edgedGraph);
    const runTwo = {
      ...sessionWithBranches(edgedGraph),
      id: "run-mount-view-2",
      branches: [{ id: "branch-c", name: "C", created_at: 0, messages: [] }],
    };
    vi.mocked(getSession).mockImplementation(
      async (id: string) => (id === "run-mount-view-2" ? runTwo : runOne) as never,
    );
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    try {
      await act(async () => {
        root.render(
          <IntlProvider locale="en" messages={enMessages}>
            <RunDetail id="run-mount-view" />
          </IntlProvider>,
        );
      });
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });

      const canvas = container.querySelector('[data-testid="worker-canvas"]');
      const panel = canvas?.parentElement;
      const fakeNode = document.createElement("div");
      fakeNode.className = "react-flow__node";
      fakeNode.dataset.id = "A";
      panel!.appendChild(fakeNode);
      await act(async () => {
        fakeNode.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });
      expect(
        container.querySelector('[data-testid="run-detail-selected-node"]')?.textContent,
      ).toContain("A");

      await act(async () => {
        root.render(
          <IntlProvider locale="en" messages={enMessages}>
            <RunDetail id="run-mount-view-2" />
          </IntlProvider>,
        );
      });
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(container.querySelector('[data-testid="run-detail-selected-node"]')).toBeNull();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});

// ─── ADR-0113 rows 8 & 9 — run controls, mounted ────────────────────────────

describe("history/RunDetail.tsx — pause/resume/steer controls, mounted", () => {
  beforeEach(async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    Element.prototype.scrollIntoView = vi.fn();
    // Everything in this block describes the surface as it behaves once some
    // verb has a backing command. That is the state in which D4's rule
    // applies: a verb the engine cannot carry out is shown and disabled with
    // its reason, never hidden, so a deliberate constraint does not read as a
    // missing feature. The separate case — no verb backed at all — is covered
    // below, and is the only reason these need to opt in.
    const { hasAnyExecutablePath } = await import("@/lib/runControls");
    vi.mocked(hasAnyExecutablePath).mockReturnValue(true);
  });

  // Nothing in the vitest config resets mocks between tests, so the three
  // wrappers the run-swap test overrides are put back to the real
  // implementations here. Without this, one test's stand-in registry would
  // silently become every later test's premise.
  afterEach(async () => {
    vi.unstubAllGlobals();
    const actual = await vi.importActual<typeof import("@/lib/runControls")>("@/lib/runControls");
    const { applyExecutablePath, proposeRunControl, confirmRunControl } =
      await import("@/lib/runControls");
    vi.mocked(applyExecutablePath).mockImplementation(actual.applyExecutablePath);
    vi.mocked(proposeRunControl).mockImplementation(actual.proposeRunControl);
    vi.mocked(confirmRunControl).mockImplementation(actual.confirmRunControl);
  });

  const flatGraph = { name: "run", description: "", nodes: [], edges: [] };

  // has_control_consumer mirrors what services/sessions.py projects for every
  // session: true for flow and play unconditionally, and for an agent run only
  // when a lionagi runner owns it and declared that it drains controls.
  function sessionOf(invocationKind: string | null, hasControlConsumer = true) {
    return {
      id: "run-mount-controls",
      name: "run-mount-controls",
      created_at: 0,
      updated_at: 0,
      status: "running",
      invocation_kind: invocationKind,
      has_control_consumer: hasControlConsumer,
      // A control is authorized against the project of the conversation it is
      // proposed in, which comes from the run. A session without one is its
      // own case, covered separately below.
      project: "studio",
      branches: [],
      graph: flatGraph,
    };
  }

  async function mountRunDetail(session: unknown) {
    const [{ getSession }, { default: RunDetail }] = await Promise.all([
      import("@/lib/api"),
      import("./RunDetail"),
    ]);
    vi.mocked(getSession).mockResolvedValue(session as never);
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <RunDetail id="run-mount-controls" />
        </IntlProvider>,
      );
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    return {
      container,
      unmount: () => {
        act(() => root.unmount());
        container.remove();
      },
    };
  }

  it("row 8: pause is shown and DISABLED on an agent run — never hidden", async () => {
    const { container, unmount } = await mountRunDetail(sessionOf("agent"));
    try {
      const pauseButton = container.querySelector<HTMLButtonElement>(
        '[data-testid="run-controls-pause"]',
      );
      expect(pauseButton).not.toBeNull();
      expect(pauseButton?.disabled).toBe(true);
      // Resume is not a listed agent capability at all — not offered, not
      // just disabled.
      expect(container.querySelector('[data-testid="run-controls-resume"]')).toBeNull();
    } finally {
      unmount();
    }
  });

  it("enables backed flow controls while keeping resume state-aware", async () => {
    const { container, unmount } = await mountRunDetail(sessionOf("flow"));
    try {
      const pause = container.querySelector<HTMLButtonElement>(
        '[data-testid="run-controls-pause"]',
      );
      const resume = container.querySelector<HTMLButtonElement>(
        '[data-testid="run-controls-resume"]',
      );
      const steer = container.querySelector<HTMLButtonElement>(
        '[data-testid="run-controls-steer"]',
      );
      expect(pause?.disabled).toBe(false);
      expect(resume?.disabled).toBe(true);
      expect(steer?.disabled).toBe(false);
    } finally {
      unmount();
    }
  });

  it("states the run-state refusal in text, not only in a tooltip", async () => {
    const { container, unmount } = await mountRunDetail(sessionOf("flow"));
    try {
      const reason = container.querySelector('[data-testid="run-controls-reason-not-paused"]');
      expect(reason).not.toBeNull();
      expect(reason?.textContent).toBe("The run is not paused.");
    } finally {
      unmount();
    }
  });

  it("offers steering as the supported alternative to pausing an agent turn", async () => {
    const { container, unmount } = await mountRunDetail(sessionOf("agent"));
    try {
      const panel = container.querySelector('[data-testid="run-controls"]');
      expect(panel?.textContent).toContain("steer instead");
      expect(
        container.querySelector<HTMLButtonElement>('[data-testid="run-controls-steer"]')?.disabled,
      ).toBe(false);
    } finally {
      unmount();
    }
  });

  it("disables steering for an agent run no runner owns, and says why", async () => {
    // A mirrored or imported session carries invocation_kind "agent" like a
    // live one. The server refuses every control queued against it, so an
    // enabled steer here would be a button that can never queue anything.
    const { container, unmount } = await mountRunDetail(sessionOf("agent", false));
    try {
      const steer = container.querySelector<HTMLButtonElement>(
        '[data-testid="run-controls-steer"]',
      );
      expect(steer).not.toBeNull();
      expect(steer?.disabled).toBe(true);
      const reason = container.querySelector(
        '[data-testid="run-controls-reason-no-live-consumer"]',
      );
      expect(reason?.textContent).toBe(
        "This run is a mirrored or imported session, so no runner would deliver a control.",
      );
    } finally {
      unmount();
    }
  });

  it("refuses every control on a completed_empty run, which the server will not admit", async () => {
    // completed_empty is a valid terminal status the display mapping does not
    // recognize, so it used to fold into "running" and leave these controls
    // enabled against a run the server refuses with not_running.
    const { container, unmount } = await mountRunDetail({
      ...sessionOf("flow"),
      status: "completed_empty",
    });
    try {
      expect(
        container.querySelector<HTMLButtonElement>('[data-testid="run-controls-pause"]')?.disabled,
      ).toBe(true);
      expect(
        container.querySelector<HTMLButtonElement>('[data-testid="run-controls-steer"]')?.disabled,
      ).toBe(true);
    } finally {
      unmount();
    }
  });

  it("refuses every control on a run with no project, and says which limit it is", async () => {
    // A control is authorized against the project of the conversation it is
    // proposed in, and a run with no project leaves that conversation nothing
    // to be scoped to. The server rejects it before proposing anything, so an
    // enabled control here is one that can never succeed.
    const { project: _omitted, ...withoutProject } = sessionOf("flow");
    const { container, unmount } = await mountRunDetail(withoutProject);
    try {
      expect(
        container.querySelector<HTMLButtonElement>('[data-testid="run-controls-pause"]')?.disabled,
      ).toBe(true);
      expect(
        container.querySelector<HTMLButtonElement>('[data-testid="run-controls-steer"]')?.disabled,
      ).toBe(true);
      const reason = container.querySelector(
        '[data-testid="run-controls-reason-no-project-scope"]',
      );
      expect(reason?.textContent).toBe(
        "This run has no project, so a control cannot be authorized for it.",
      );
    } finally {
      unmount();
    }
  });

  it("a run the server reports as paused offers Resume on a fresh mount", async () => {
    // The state this closes: pause lived only in component state, so a reload
    // of a still-paused run came back reading "not paused" — Pause enabled,
    // Resume refused, and no way left to release the gate.
    const { container, unmount } = await mountRunDetail({
      ...sessionOf("flow"),
      pause_is_held: true,
    });
    try {
      expect(
        container.querySelector<HTMLButtonElement>('[data-testid="run-controls-resume"]')?.disabled,
      ).toBe(false);
      expect(
        container.querySelector<HTMLButtonElement>('[data-testid="run-controls-pause"]')?.disabled,
      ).toBe(true);
    } finally {
      unmount();
    }
  });

  it("a response that never carried the capability field does not enable steering", async () => {
    // Absent is not evidence of a capability: the strict compare in RunDetail
    // is what keeps a missing field from reading as permission.
    const { has_control_consumer: _omitted, ...withoutField } = sessionOf("agent");
    const { container, unmount } = await mountRunDetail(withoutField);
    try {
      expect(
        container.querySelector<HTMLButtonElement>('[data-testid="run-controls-steer"]')?.disabled,
      ).toBe(true);
    } finally {
      unmount();
    }
  });

  it("no control panel renders for a kind the control poller does not drain (e.g. show-play)", async () => {
    const { container, unmount } = await mountRunDetail(sessionOf("show-play"));
    try {
      expect(container.querySelector('[data-testid="run-controls"]')).toBeNull();
    } finally {
      unmount();
    }
  });

  // The state the surface is actually in today. Every verb above is disabled
  // for want of a backing command, and a panel in which nothing can ever be
  // clicked reads as a broken feature rather than an unbuilt one. So while no
  // verb is backed the section is not rendered at all. This is scoped to that
  // case and does not weaken the rule above: the moment one command type
  // exists the section returns, and a verb the engine still cannot carry out
  // goes back to being shown and disabled with its reason.
  it("renders no control section at all while no verb has a backing command", async () => {
    const { hasAnyExecutablePath } = await import("@/lib/runControls");
    vi.mocked(hasAnyExecutablePath).mockReturnValue(false);
    const { container, unmount } = await mountRunDetail(sessionOf("flow"));
    try {
      expect(container.querySelector('[data-testid="run-controls"]')).toBeNull();
      // and specifically not the disabled-with-a-reason rendering
      expect(
        container.querySelector('[data-testid="run-controls-reason-no-executable-path"]'),
      ).toBeNull();
    } finally {
      unmount();
    }
  });

  // The pane is reused across runs: the id-change effect cleared session,
  // graph, signals, and selection, and left the pause request behind, so run
  // B derived its control state from a pause only run A had ever received.
  // Once a command exists to carry the verb out, that is a resume dispatched
  // against B's id for A's pause. No unit test of the phase function can see
  // this — what is wrong is which state survives the swap — so the assertion
  // has to be mounted and has to swap.
  it("a pause accepted on one run does not carry into the next run shown in the same pane", async () => {
    const [{ getSession }, { default: RunDetail }, controls] = await Promise.all([
      import("@/lib/api"),
      import("./RunDetail"),
      import("@/lib/runControls"),
    ]);

    // Stands in for a registry with a backing command. Left real, every verb
    // is refused before it is offered and the pause this test is about can
    // never be accepted at all.
    vi.mocked(controls.applyExecutablePath).mockImplementation((_verb, state) => state);
    vi.mocked(controls.proposeRunControl).mockResolvedValue({
      conversationId: "conv-run-swap",
      proposal: {
        id: "prop-run-swap",
        commandType: "pause_run",
        summary: "Pause the run",
        commandHash: "hash-run-swap",
        target: null,
      },
    } as never);
    vi.mocked(controls.confirmRunControl).mockResolvedValue({} as never);
    vi.mocked(getSession).mockImplementation((async (runId: string) => ({
      id: runId,
      name: runId,
      created_at: 0,
      updated_at: 0,
      status: "running",
      invocation_kind: "flow",
      project: "studio",
      branches: [],
      graph: flatGraph,
    })) as never);

    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const show = async (runId: string) => {
      await act(async () => {
        root.render(
          <IntlProvider locale="en" messages={enMessages}>
            <RunDetail id={runId} />
          </IntlProvider>,
        );
      });
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
    };
    const pauseLabel = () =>
      container.querySelector('[data-testid="run-controls-pause"]')?.textContent;
    const stillPausing = () =>
      container.querySelector('[data-testid="run-controls-reason-still-pausing"]');

    try {
      await show("run-swap-a");
      expect(pauseLabel()).toBe("Pause");
      expect(stillPausing()).toBeNull();

      // Accept a pause on run A through the surface — propose, then confirm —
      // rather than by setting the state this test is about.
      await act(async () => {
        container.querySelector<HTMLButtonElement>('[data-testid="run-controls-pause"]')?.click();
      });
      const confirmYes = container.querySelector<HTMLButtonElement>(
        '[data-testid="run-controls-confirm"] button',
      );
      expect(confirmYes, "no confirm dialog appeared after proposing a pause").not.toBeNull();
      await act(async () => {
        confirmYes?.click();
      });
      // This run has no authored graph and no signals yet, so nothing can say
      // how much is still in flight: the phase is "pausing", never "paused".
      expect(pauseLabel()).toBe("Pausing…");
      expect(stillPausing()).not.toBeNull();

      // Same pane, next run. Nothing about B was ever paused.
      await show("run-swap-b");
      expect(pauseLabel()).toBe("Pause");
      expect(stillPausing()).toBeNull();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});

function openConversationTab(container: HTMLElement): void {
  const tab = container.querySelector<HTMLButtonElement>('[id$="-tab-conversation"]');
  if (!tab) throw new Error("conversation tab not rendered");
  act(() => {
    tab.click();
  });
}

describe("history/RunDetail.tsx — a tool result nobody read is not a tool call that worked", () => {
  // The server withholds a message payload past its per-row size ceiling and
  // marks the row `content_withheld`. Every consumer here decides success by
  // reading the output, and a withheld output is an empty string, so without
  // the flag a call whose result nobody has seen renders with a green check.
  const withheldBranch = (contentWithheld: boolean) => ({
    id: "branch-withheld",
    name: "worker",
    created_at: 10,
    message_total: 2,
    messages: [
      {
        id: "req-1",
        role: "action",
        content: {
          function: "Bash",
          arguments: { command: "ls" },
          action_response_id: "resp-1",
        },
        sender: "worker",
        timestamp: 11,
        lion_class: "ActionRequest",
      },
      {
        id: "resp-1",
        role: "action",
        content: contentWithheld ? null : { function: "Bash", output: "a.txt" },
        content_withheld: contentWithheld,
        sender: "tool",
        timestamp: 12,
        lion_class: "ActionResponse",
      },
    ],
  });

  it("marks a paired call whose response payload was withheld", async () => {
    const { branchToRunStep } = await import("./RunDetail");
    const step = branchToRunStep(withheldBranch(true) as never, "completed");
    const [call] = (step.messages ?? []).filter((m) => m.role === "tool_call");
    expect(call.status).toBe("withheld");
  });

  it("still reports an ordinary call as ok", async () => {
    // Control: "withheld" has to be reachable only through the flag, or the
    // assertion above is satisfied by a status that is always withheld.
    const { branchToRunStep } = await import("./RunDetail");
    const step = branchToRunStep(withheldBranch(false) as never, "completed");
    const [call] = (step.messages ?? []).filter((m) => m.role === "tool_call");
    expect(call.status).toBe("ok");
  });

  // A withheld REQUEST is the harder half. Its payload is what carries the
  // function name, the arguments and the forward link to its response, so a
  // consumer reading only the response's flag sees an ordinary call with a
  // blank name, and the response it could no longer point at renders as a
  // second one. Two green checks, for one call nobody could read.
  const withheldRequestBranch = (output = "a.txt") => ({
    id: "branch-withheld-req",
    name: "worker",
    created_at: 10,
    message_total: 2,
    messages: [
      {
        id: "req-1",
        role: "action",
        content: null,
        content_withheld: true,
        sender: "worker",
        timestamp: 11,
        lion_class: "ActionRequest",
      },
      {
        id: "resp-1",
        role: "action",
        content: { function: "Bash", output, action_request_id: "req-1" },
        sender: "tool",
        timestamp: 12,
        lion_class: "ActionResponse",
      },
    ],
  });

  it("marks a call whose own request payload was withheld", async () => {
    const { branchToRunStep } = await import("./RunDetail");
    const step = branchToRunStep(withheldRequestBranch() as never, "completed");
    const calls = (step.messages ?? []).filter((m) => m.role === "tool_call");
    expect(calls.map((c) => c.status)).toEqual(["withheld"]);
  });

  it("reports the failure when a withheld request's response came back and said error", async () => {
    // The two halves are withheld independently, so the request can be past
    // the ceiling while the reply is decoded and readable. "not read" is then
    // the one thing the row is not: somebody did read this, and it failed.
    // Answering with the badge would hide a visible failure behind a shrug,
    // and the output that says so is rendered right beside it.
    const { branchToRunStep } = await import("./RunDetail");
    const step = branchToRunStep(
      withheldRequestBranch("Error: command not found") as never,
      "completed",
    );
    const calls = (step.messages ?? []).filter((m) => m.role === "tool_call");
    expect(calls.map((c) => c.status)).toEqual(["error"]);
  });

  it("keeps the withheld badge when that same response carries no failure", async () => {
    // Control, and the reason the fixtures differ by one field: an errored
    // reply must be what produces "error" above, not the withheld request.
    // The request is still unread here, which is what the blank function name
    // on the row needs explained, so the badge stays.
    const { branchToRunStep } = await import("./RunDetail");
    const step = branchToRunStep(withheldRequestBranch("a.txt") as never, "completed");
    const calls = (step.messages ?? []).filter((m) => m.role === "tool_call");
    expect(calls.map((c) => c.status)).toEqual(["withheld"]);
  });

  it("pairs a withheld request with its response from the response's own end", async () => {
    // One row, not two: the response names its request in a payload the
    // request's withholding cannot reach, so the pairing survives it.
    const { branchToRunStep } = await import("./RunDetail");
    const step = branchToRunStep(withheldRequestBranch() as never, "completed");
    expect((step.messages ?? []).filter((m) => m.role === "tool_call")).toHaveLength(1);
  });

  it("marks an unpaired response whose own payload was withheld", async () => {
    const { branchToRunStep } = await import("./RunDetail");
    const branch = withheldBranch(true) as never as { messages: unknown[] };
    const step = branchToRunStep(
      { ...branch, messages: [branch.messages[1]] } as never,
      "completed",
    );
    const [call] = (step.messages ?? []).filter((m) => m.role === "tool_call");
    expect(call.status).toBe("withheld");
  });

  it("renders the withheld badge instead of the success check", () => {
    const withheld = {
      step: "s1",
      status: "completed",
      timestamp: 1,
      messages: [
        {
          role: "tool_call",
          function: "Bash",
          summary: "ls",
          output: "",
          status: "withheld",
          timestamp: 1,
        },
      ],
    };
    const { container } = renderRunStepCards([withheld as never], true);
    openConversationTab(container);
    expect(container.textContent).toContain("not read");
  });

  it("does not render the withheld badge for an ordinary call", () => {
    // Control for the render: "not read" must be absent when the status is ok,
    // or its presence above says nothing about the status.
    const ok = {
      step: "s1",
      status: "completed",
      timestamp: 1,
      messages: [
        {
          role: "tool_call",
          function: "Bash",
          summary: "ls",
          output: "a.txt",
          status: "ok",
          timestamp: 1,
        },
      ],
    };
    const { container } = renderRunStepCards([ok as never], true);
    openConversationTab(container);
    // The tool call is on screen -- this is the same panel the assertion above
    // reads, so its silence is about the status and not about the tab.
    expect(container.textContent).toContain("ls");
    expect(container.textContent).not.toContain("not read");
  });
});

describe("history/RunDetail.tsx — a withheld row is still a row", () => {
  // Both halves of one call refused. The request has no function name, no
  // arguments and no forward link; the response has no back link. Every
  // pairing the transcript knows about lives in a payload neither of them
  // still has, so without the ids the server lifts out of the row itself,
  // one call arrives as two unrelated rows.
  const bothWithheldBranch = (liftIds: boolean) => ({
    id: "branch-both-withheld",
    name: "worker",
    created_at: 10,
    message_total: 2,
    messages: [
      {
        id: "req-1",
        role: "action",
        content: null,
        content_withheld: true,
        ...(liftIds ? { action_response_id: "resp-1" } : {}),
        sender: "worker",
        timestamp: 11,
        lion_class: "ActionRequest",
      },
      {
        id: "resp-1",
        role: "action",
        content: null,
        content_withheld: true,
        ...(liftIds ? { action_request_id: "req-1" } : {}),
        sender: "tool",
        timestamp: 12,
        lion_class: "ActionResponse",
      },
    ],
  });

  it("renders one row when a call has both halves withheld", async () => {
    const { branchToRunStep } = await import("./RunDetail");
    const step = branchToRunStep(bothWithheldBranch(true) as never, "completed");
    const calls = (step.messages ?? []).filter((m) => m.role === "tool_call");
    expect(calls).toHaveLength(1);
    expect(calls[0].status).toBe("withheld");
  });

  // The two lifted ids are two independent routes to the same pairing, so a
  // fixture carrying both cannot say whether either one works. These strip one
  // route each. A row can be withheld on one side and hydrated on the other,
  // which is why both routes exist rather than one.
  const oneSidedBranch = (side: "request" | "response") => {
    const branch = bothWithheldBranch(true);
    const [request, response] = branch.messages as Record<string, unknown>[];
    if (side === "request") delete response.action_request_id;
    else delete request.action_response_id;
    return branch;
  };

  it("pairs a both-withheld call from the request's lifted forward link alone", async () => {
    const { branchToRunStep } = await import("./RunDetail");
    const step = branchToRunStep(oneSidedBranch("request") as never, "completed");
    expect((step.messages ?? []).filter((m) => m.role === "tool_call")).toHaveLength(1);
  });

  it("pairs a both-withheld call from the response's lifted back link alone", async () => {
    const { branchToRunStep } = await import("./RunDetail");
    const step = branchToRunStep(oneSidedBranch("response") as never, "completed");
    expect((step.messages ?? []).filter((m) => m.role === "tool_call")).toHaveLength(1);
  });

  it("splits the same call into two rows without the lifted ids", async () => {
    // Control: the single row above has to come from the ids and not from
    // some other collapse, or the assertion passes for the wrong reason.
    const { branchToRunStep } = await import("./RunDetail");
    const step = branchToRunStep(bothWithheldBranch(false) as never, "completed");
    expect((step.messages ?? []).filter((m) => m.role === "tool_call")).toHaveLength(2);
  });

  // Withholding is decided by payload size, not by message kind, so a system,
  // user or assistant message hits it too. Each of the three readers fails
  // differently on an empty payload and all three fail silently.
  const nonActionBranch = (withheld: boolean) => ({
    id: "branch-non-action",
    name: "worker",
    created_at: 10,
    message_total: 3,
    messages: [
      {
        id: "sys-1",
        role: "system",
        content: withheld ? null : { system_message: "you are a worker" },
        content_withheld: withheld,
        sender: "system",
        timestamp: 11,
        lion_class: "System",
      },
      {
        id: "usr-1",
        role: "user",
        content: withheld ? null : { instruction: "do the thing" },
        content_withheld: withheld,
        sender: "user",
        timestamp: 12,
        lion_class: "Instruction",
      },
      {
        id: "asst-1",
        role: "assistant",
        content: withheld ? null : { assistant_response: "done" },
        content_withheld: withheld,
        sender: "worker",
        timestamp: 13,
        lion_class: "AssistantResponse",
      },
    ],
  });

  it("keeps a withheld system, user and assistant message as one marked row each", async () => {
    const { branchToRunStep } = await import("./RunDetail");
    const step = branchToRunStep(nonActionBranch(true) as never, "completed");
    const messages = step.messages ?? [];
    expect(messages.map((m) => m.role)).toEqual(["system", "user", "assistant"]);
    expect(messages.every((m) => m.withheld === true)).toBe(true);
    // The literal "{}" is what a serialized empty payload used to render as.
    expect(messages.some((m) => m.content === "{}")).toBe(false);
  });

  it("leaves ordinary system, user and assistant messages unmarked", async () => {
    const { branchToRunStep } = await import("./RunDetail");
    const step = branchToRunStep(nonActionBranch(false) as never, "completed");
    const messages = step.messages ?? [];
    expect(messages.map((m) => m.content)).toEqual(["you are a worker", "do the thing", "done"]);
    expect(messages.some((m) => m.withheld)).toBe(false);
  });

  it("renders a withheld assistant turn as unread rather than as a blank one", () => {
    const step = {
      step: "s1",
      status: "completed",
      timestamp: 1,
      messages: [{ role: "assistant", content: "", withheld: true, timestamp: 1 }],
    };
    const { container } = renderRunStepCards([step as never], true);
    openConversationTab(container);
    expect(container.textContent).toContain("not read");
  });
});
