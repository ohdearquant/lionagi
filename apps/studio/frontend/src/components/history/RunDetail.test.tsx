/**
 * RunDetail contract tests.
 *
 * Verifies:
 * - RunDetail.tsx exists and exports a default component
 * - It does not import Drawer (master-detail doctrine)
 */

import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

const HISTORY_DIR = path.resolve(__dirname);

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

// ─── Authored graph is never transitively reduced ────────────────────────────
// runGraph is Studio's persisted early_graph — the exact graph the designer
// authored, edges and conditions included. Applying transitiveReduce() to it
// would silently drop an authored conditional edge (e.g. A→B, B→C, and a
// conditional A→C) whenever the runtime happens to also reach C via B — the
// runtime emitter's depends_on is a predecessor list, not proof an edge is
// synthetic. Reduction stays scoped to buildOperationGraph's runtime-derived
// opGraph (whose edges genuinely are a raw ancestor list).

describe("history/RunDetail.tsx — authored run graph is rendered unreduced", () => {
  const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");

  it("does not import transitiveReduce", () => {
    expect(src).not.toMatch(/transitiveReduce/);
  });

  it("passes runGraph directly to WorkerCanvas, not a reduced copy", () => {
    expect(src).toMatch(/graph={runGraph}/);
  });
});

describe("transitiveReduce (lib/operationGraph) — why RunDetail must not apply it to runGraph", () => {
  it("would drop an authored conditional A→C that transitiveReduce sees as redundant via A→B→C", async () => {
    const { transitiveReduce } = await import("@/lib/operationGraph");

    // Mirrors an authored WorkerGraph: A→B, B→C, and a conditional A→C.
    const authoredEdges = [
      { id: "e-ab", source: "A", target: "B" },
      { id: "e-bc", source: "B", target: "C" },
      { id: "e-ac", source: "A", target: "C", condition: "score > 0.8" },
    ];

    // What the old code did (reduce the authored graph): loses the
    // conditional edge, because C is reachable from A through B.
    const wouldHaveReduced = transitiveReduce(authoredEdges);
    expect(wouldHaveReduced.find((e) => e.id === "e-ac")).toBeUndefined();

    // What RunDetail does now: pass the authored edges through unchanged,
    // so the conditional A→C survives.
    const rendered = authoredEdges;
    expect(rendered.find((e) => e.id === "e-ac")).toBeDefined();
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
  it("decode site normalizes omitted graph.edges to [] before setRunGraph", () => {
    const src = fs.readFileSync(path.join(HISTORY_DIR, "RunDetail.tsx"), "utf-8");
    expect(src).toMatch(/edges:\s*graph\.edges\s*\?\?\s*\[\]/);
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
