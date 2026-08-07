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
    // The reviewer's duplicate-implementer scenario: {name:"implementer-2",
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

  it("hasFailure trips the unmissable-failure header tone when any node is failed or escalated", async () => {
    const { computeReconciledNodeStatuses, computeProgressCountsForGraph } =
      await import("./RunDetail");
    const reconciled = computeReconciledNodeStatuses(graph, { a: "escalated" }, true);
    const counts = computeProgressCountsForGraph(graph, reconciled);
    expect(counts?.hasFailure).toBe(true);
    expect(counts?.failed).toBe(1);
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
