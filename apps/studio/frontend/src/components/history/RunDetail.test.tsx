/**
 * RunDetail contract tests.
 *
 * Verifies:
 * - RunDetail.tsx exists and exports a default component
 * - It does not import Drawer (master-detail doctrine)
 */

import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
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

  it("a single-node authored graph is never considered edgeless (nothing to draw an edge between)", async () => {
    const { shouldRenderAuthoredGraph } = await import("./RunDetail");
    const singleNode = { nodes: [{ id: "a" }], edges: [] };
    const opGraphWithEdges = { edges: [{ source: "op-a", target: "op-b" }] };
    expect(shouldRenderAuthoredGraph(singleNode, opGraphWithEdges)).toBe(true);
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
    ).toEqual({ toolCallCount: 21_741, errorCount: 42 });
  });

  it("does not select recent-qualified labels for partial message windows", () => {
    const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");
    const start = src.indexOf("function OverviewSection");
    const end = src.indexOf("// ── Branches section", start);
    const overview = src.slice(start, end);
    expect(overview).not.toMatch(/statToolCallsRecent|statErrorsRecent/);
  });
});

// ─── NodeEscalated route=notify badge ──────────────────────────────────────────
// A soft ("fyi" urgency) EscalationRequest resolves to route="notify" and
// fires NodeEscalated purely for observability — the node itself keeps
// working. The per-event timeline badge must not label that "escalated"
// (error tone) the same as a real, terminal escalation.

describe("history/RunDetail.tsx — badgeForEvent (NodeEscalated route=notify)", () => {
  it("labels a route=notify NodeEscalated as notify, not escalated", async () => {
    const { badgeForEvent } = await import("./RunDetail");
    const badge = badgeForEvent({
      id: "1",
      session_id: "s1",
      seq: 0,
      kind: "NodeEscalated",
      op_id: "op-a",
      ts: 1,
      payload: { route: "notify" },
    });
    expect(badge.label).toBe("notify");
    expect(badge.tone).not.toMatch(/error/);
  });

  it("still labels a route=higher_tier NodeEscalated as escalated", async () => {
    const { badgeForEvent } = await import("./RunDetail");
    const badge = badgeForEvent({
      id: "1",
      session_id: "s1",
      seq: 0,
      kind: "NodeEscalated",
      op_id: "op-a",
      ts: 1,
      payload: { route: "higher_tier" },
    });
    expect(badge.label).toBe("escalated");
    expect(badge.tone).toMatch(/error/);
  });

  it("still labels a bare NodeEscalated (no route) as escalated — back-compat", async () => {
    const { badgeForEvent } = await import("./RunDetail");
    const badge = badgeForEvent({
      id: "1",
      session_id: "s1",
      seq: 0,
      kind: "NodeEscalated",
      op_id: "op-a",
      ts: 1,
      payload: {},
    });
    expect(badge.label).toBe("escalated");
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
