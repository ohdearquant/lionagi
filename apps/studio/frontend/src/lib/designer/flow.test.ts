/**
 * Flow derivation contract tests.
 *
 * Two layers of invariants over deriveFlow(ENGINE_TOPOLOGIES[kind]):
 *  - semantic: spawn rules, observes, signal index — these encode what the
 *    engine sources actually do and must survive any layout rework;
 *  - geometric: layering monotonicity, serpentine wrap, edge classification,
 *    finite/positive extents — these keep the projection readable.
 */

import { describe, it, expect } from "vitest";
import { ENGINE_KINDS, ENGINE_TOPOLOGIES } from "./topology";
import { deriveFlow, HANDOFF, QUIESCENCE, SIGNAL_PALETTE } from "./flow";
import type { FlowModel } from "./flow";

const models = Object.fromEntries(
  ENGINE_KINDS.map((k) => [k, deriveFlow(ENGINE_TOPOLOGIES[k])]),
) as Record<(typeof ENGINE_KINDS)[number], FlowModel>;

const layerOf = (m: FlowModel) => new Map(m.nodes.map((n) => [n.id, n.layer]));

// ─── Entity collapse ──────────────────────────────────────────────────────────

describe("deriveFlow — entity collapse", () => {
  it("one node per entity (groups collapse their members)", () => {
    expect(models.research.nodes).toHaveLength(3); // topic · exploration · synthesize
    expect(models.review.nodes).toHaveLength(4);
    expect(models.coding.nodes).toHaveLength(6);
    expect(models.hypothesis.nodes).toHaveLength(9);
    expect(models.planning.nodes).toHaveLength(4);
  });

  it("node ids are unique", () => {
    for (const kind of ENGINE_KINDS) {
      const ids = models[kind].nodes.map((n) => n.id);
      expect(new Set(ids).size).toBe(ids.length);
    }
  });

  it("research exploration group carries its members in stage order", () => {
    const g = models.research.nodes.find((n) => n.kind === "group");
    expect(g?.id).toBe("exploration");
    expect(g?.label).toBe("exploration node");
    expect(g?.members?.map((m) => m.stage.id)).toEqual(["researcher", "analyst", "critic"]);
  });

  it("group member rows stack in order inside the card", () => {
    const g = models.research.nodes.find((n) => n.kind === "group")!;
    const rows = g.members!;
    for (let i = 0; i < rows.length; i++) {
      expect(rows[i].relY).toBeGreaterThan(0);
      expect(rows[i].relY).toBeLessThan(g.h);
      if (i > 0) expect(rows[i].relY).toBeGreaterThan(rows[i - 1].relY);
    }
  });
});

// ─── Ports ────────────────────────────────────────────────────────────────────

describe("deriveFlow — ports", () => {
  it("every edge anchors on an existing out-port and in-port of its endpoints", () => {
    for (const kind of ENGINE_KINDS) {
      const m = models[kind];
      const byId = new Map(m.nodes.map((n) => [n.id, n]));
      for (const e of m.edges) {
        const name = e.kind === "quiescence" ? QUIESCENCE : (e.signal ?? HANDOFF);
        expect(byId.get(e.from)!.outPorts.map((p) => p.name)).toContain(name);
        expect(byId.get(e.to)!.inPorts.map((p) => p.name)).toContain(name);
      }
    }
  });

  it("signal-less seq hand-offs ride the handoff port — the final response is an event", () => {
    const m = models.research;
    const topic = m.nodes.find((n) => n.id === "topic")!;
    const exploration = m.nodes.find((n) => n.id === "exploration")!;
    expect(topic.outPorts.map((p) => p.name)).toContain(HANDOFF);
    expect(exploration.inPorts.map((p) => p.name)).toContain(HANDOFF);
    const handoffPort = topic.outPorts.find((p) => p.name === HANDOFF)!;
    expect(handoffPort.system).toBe(true);
  });

  it("port rows sit inside the card and inputs stack above outputs", () => {
    for (const kind of ENGINE_KINDS) {
      for (const n of models[kind].nodes) {
        const all = [...n.inPorts, ...n.outPorts];
        for (const p of all) {
          expect(p.relY).toBeGreaterThan(0);
          expect(p.relY).toBeLessThan(n.h);
        }
        if (n.inPorts.length > 0 && n.outPorts.length > 0) {
          const maxIn = Math.max(...n.inPorts.map((p) => p.relY));
          const minOut = Math.min(...n.outPorts.map((p) => p.relY));
          expect(minOut).toBeGreaterThan(maxIn);
        }
      }
    }
  });

  it("declared emissions appear as out-ports even when unconsumed", () => {
    const exploration = models.research.nodes.find((n) => n.id === "exploration")!;
    expect(exploration.outPorts.map((p) => p.name)).toContain("ContradictionFound");
  });

  it("planning workers node is typed as a reactive DAG", () => {
    const workers = models.planning.nodes.find((n) => n.id === "workers")!;
    expect(workers.typeLabel).toBe("reactive DAG");
    expect(workers.inPorts.map((p) => p.name)).toContain("SpawnRequest");
    expect(workers.outPorts.map((p) => p.name)).toContain("SpawnRequest");
  });
});

// ─── Layering ─────────────────────────────────────────────────────────────────

describe("deriveFlow — layering", () => {
  it("forward, wrap and quiescence edges strictly increase causal layer", () => {
    for (const kind of ENGINE_KINDS) {
      const m = models[kind];
      const layers = layerOf(m);
      for (const e of m.edges) {
        if (e.kind === "forward" || e.kind === "wrap" || e.kind === "quiescence") {
          expect(layers.get(e.to)!).toBeGreaterThan(layers.get(e.from)!);
        }
        if (e.kind === "self") expect(e.from).toBe(e.to);
        if (e.kind === "loop") expect(layers.get(e.to)!).toBeLessThanOrEqual(layers.get(e.from)!);
      }
    }
  });

  it("input stages anchor layer 0", () => {
    for (const kind of ENGINE_KINDS) {
      const m = models[kind];
      const input = m.nodes.find((n) => n.stages[0].kind === "input")!;
      expect(input.layer).toBe(0);
    }
  });

  it("coding chain fits one row; hypothesis cascade wraps to two", () => {
    expect(models.coding.nodes.every((n) => n.row === 0)).toBe(true);
    const hypoRows = new Set(models.hypothesis.nodes.map((n) => n.row));
    expect(hypoRows).toEqual(new Set([0, 1]));
    expect(models.hypothesis.nodes.filter((n) => n.row === 1)).toHaveLength(4);
  });
});

// ─── Edge classification ──────────────────────────────────────────────────────

describe("deriveFlow — edge classification", () => {
  it("research: inner group seq drops, loops re-enter as self edges, quiescence hand-off", () => {
    const m = models.research;
    expect(m.edges).toHaveLength(4);
    const selfs = m.edges.filter((e) => e.kind === "self");
    expect(selfs.map((e) => e.signal).sort()).toEqual(["ContradictionFound", "DepthRequested"]);
    expect(selfs.every((e) => e.from === "exploration")).toBe(true);
    const q = m.edges.find((e) => e.kind === "quiescence")!;
    expect([q.from, q.to]).toEqual(["exploration", "synthesize"]);
    expect(q.signal).toBe(QUIESCENCE);
    // member stage ids never appear as edge endpoints
    for (const e of m.edges) {
      expect(["researcher", "analyst", "critic"]).not.toContain(e.from);
      expect(["researcher", "analyst", "critic"]).not.toContain(e.to);
    }
  });

  it("coding: one fix loop, no wrap, gate condition preserved", () => {
    const m = models.coding;
    expect(m.edges).toHaveLength(6);
    const loops = m.edges.filter((e) => e.kind === "loop");
    expect(loops).toHaveLength(1);
    expect(loops[0]).toMatchObject({
      from: "test",
      to: "implement",
      signal: "TestsRan",
      judgeGated: true,
    });
    expect(m.edges.some((e) => e.kind === "wrap")).toBe(false);
    const gate = m.edges.find((e) => e.from === "test" && e.to === "verify")!;
    expect(gate.kind).toBe("forward");
    expect(gate.condition).toBe("passed = true");
  });

  it("hypothesis: cascade wraps once, follow-up loop returns to extract", () => {
    const m = models.hypothesis;
    expect(m.edges).toHaveLength(9);
    const wraps = m.edges.filter((e) => e.kind === "wrap");
    expect(wraps).toHaveLength(1);
    expect([wraps[0].from, wraps[0].to]).toEqual(["design", "validate"]);
    const loops = m.edges.filter((e) => e.kind === "loop");
    expect(loops).toHaveLength(1);
    expect(loops[0]).toMatchObject({ from: "apply", to: "extract", signal: "FindingPosted" });
    const q = m.edges.find((e) => e.kind === "quiescence")!;
    expect([q.from, q.to]).toEqual(["apply", "synthesize"]);
  });

  it("planning: reactive DAG self-expansion renders as a self edge", () => {
    const selfs = models.planning.edges.filter((e) => e.kind === "self");
    expect(selfs).toHaveLength(1);
    expect(selfs[0]).toMatchObject({ from: "workers", signal: "SpawnRequest" });
  });

  it("review: reactive verify hand-off carries its signal", () => {
    const e = models.review.edges.find((x) => x.from === "review" && x.to === "verify")!;
    expect(e.kind).toBe("forward");
    expect(e.signal).toBe("IssueFound");
  });
});

// ─── Signal index ─────────────────────────────────────────────────────────────

describe("deriveFlow — signal index", () => {
  it("research lists declared emissions plus the system quiescence signal", () => {
    const names = models.research.signals.map((s) => s.name);
    expect(names).toEqual(["FindingEmitted", "DepthRequested", "ContradictionFound", QUIESCENCE]);
    const quiescence = models.research.signals.find((s) => s.name === QUIESCENCE)!;
    expect(quiescence.system).toBe(true);
    expect(quiescence.color).toBe("var(--content-muted)");
    // findings feed synthesis only — no edge, but the emission stays indexed
    const dangling = models.research.signals.find((s) => s.name === "FindingEmitted")!;
    expect(dangling.observers).toEqual([]);
    expect(dangling.emitters).toEqual(["researcher", "analyst"]);
    const contradiction = models.research.signals.find((s) => s.name === "ContradictionFound")!;
    expect(contradiction.observers).toEqual(["exploration"]);
    expect(contradiction.emitters).toEqual(["analyst"]);
  });

  it("hypothesis: eight signal classes get eight distinct palette colors", () => {
    const m = models.hypothesis;
    const emitClasses = m.signals.filter((s) => !s.system);
    expect(emitClasses).toHaveLength(8);
    const colors = emitClasses.map((s) => s.color);
    expect(new Set(colors).size).toBe(8);
    for (const c of colors) expect(SIGNAL_PALETTE).toContain(c);
    expect(m.signalColor.FindingPosted).toBe(SIGNAL_PALETTE[0]);
    expect(m.signalColor.ApplicationMapped).toBe(SIGNAL_PALETTE[7]);
  });

  it("hypothesis: implied re-post makes apply an emitter of FindingPosted", () => {
    const fp = models.hypothesis.signals.find((s) => s.name === "FindingPosted")!;
    expect(fp.emitters).toContain("seed");
    expect(fp.emitters).toContain("apply");
    expect(fp.observers).toEqual(["extract"]);
  });

  it("every signal-bearing edge uses the index color", () => {
    for (const kind of ENGINE_KINDS) {
      const m = models[kind];
      for (const e of m.edges) {
        if (e.signal && e.signal !== QUIESCENCE) {
          expect(e.color).toBe(m.signalColor[e.signal]);
        }
        if (e.kind === "quiescence") expect(e.color).toBe("var(--content-muted)");
      }
    }
  });
});

// ─── Observes ─────────────────────────────────────────────────────────────────

describe("deriveFlow — observes", () => {
  it("subscriptions are unique per target + signal + condition", () => {
    for (const kind of ENGINE_KINDS) {
      const m = models[kind];
      for (const [target, specs] of Object.entries(m.observes)) {
        const keys = specs.map((o) => `${target}:${o.signal}:${o.condition ?? ""}`);
        expect(new Set(keys).size).toBe(keys.length);
      }
    }
  });

  it("research: reactions re-entering the group observe at the group boundary", () => {
    const obs = models.research.observes;
    expect(obs.exploration?.map((o) => o.signal)).toEqual(["ContradictionFound", "DepthRequested"]);
    expect(obs.researcher).toBeUndefined();
    expect(obs.critic).toBeUndefined();
    expect(obs.synthesize?.map((o) => o.signal)).toEqual([QUIESCENCE]);
  });

  it("coding: implement observes the failed-tests loop trigger", () => {
    const obs = models.coding.observes.implement!;
    expect(obs).toHaveLength(1);
    expect(obs[0]).toMatchObject({ signal: "TestsRan", condition: "passed = false" });
  });

  it("review: verdict observes quiescence exactly once", () => {
    const obs = models.review.observes.verdict!;
    expect(obs.filter((o) => o.signal === QUIESCENCE)).toHaveLength(1);
  });
});

// ─── Spawn rules (operations-layer bridge) ────────────────────────────────────

describe("deriveFlow — spawn rules", () => {
  it("every stage of every kind gets a rule", () => {
    for (const kind of ENGINE_KINDS) {
      for (const s of ENGINE_TOPOLOGIES[kind].stages) {
        expect(models[kind].spawnRules[s.id]).toBeTruthy();
      }
    }
  });

  it("research rules", () => {
    const r = models.research.spawnRules;
    expect(r.topic).toBe("entry · once per run");
    expect(r.researcher).toBe("sequential within exploration node");
    expect(r.analyst).toBe("sequential within exploration node");
    expect(r.critic).toBe("sequential within exploration node");
    expect(r.synthesize).toBe("once · on quiescence");
    const g = models.research.nodes.find((n) => n.kind === "group")!;
    expect(g.spawnRule).toBe("spawns per ContradictionFound ∨ DepthRequested");
  });

  it("review rules", () => {
    const r = models.review.spawnRules;
    expect(r.artifact).toBe("entry · once per run");
    expect(r.review).toBe("× dimension (parallel)");
    expect(r.verify).toMatch(/^spawned per IssueFound/);
    expect(r.verdict).toBe("once · on quiescence");
  });

  it("coding rules", () => {
    const r = models.coding.spawnRules;
    expect(r.plan).toBe("once per run");
    expect(r.implement).toBe("re-runs on TestsRan · passed = false");
    expect(r.test).toBe("chained on ChangeProposed");
    expect(r.verify).toBe("once per run");
    expect(r.conclude).toBe("chained on VerifyResult");
  });

  it("hypothesis rules", () => {
    const r = models.hypothesis.spawnRules;
    expect(r.extract).toMatch(/^re-runs on FindingPosted/);
    expect(r.research).toMatch(/^spawned per QuestionRaised/);
    expect(r.hypothesize).toBe("chained on EvidenceCollected");
    expect(r.synthesize).toBe("once · on quiescence");
  });

  it("planning rules", () => {
    const r = models.planning.spawnRules;
    expect(r.orchestrate).toBe("once per run");
    expect(r.workers).toBe("× assignee (planned per run)");
    expect(r.synthesize).toBe("once · on quiescence");
  });
});

// ─── Geometry sanity ──────────────────────────────────────────────────────────

describe("deriveFlow — geometry sanity", () => {
  it("extents are positive and finite; nodes stay inside them", () => {
    for (const kind of ENGINE_KINDS) {
      const m = models[kind];
      expect(Number.isFinite(m.width) && m.width > 0).toBe(true);
      expect(Number.isFinite(m.height) && m.height > 0).toBe(true);
      for (const n of m.nodes) {
        expect(n.x).toBeGreaterThanOrEqual(0);
        expect(n.y).toBeGreaterThanOrEqual(0);
        expect(n.x + n.w).toBeLessThanOrEqual(m.width);
        expect(n.y + n.h).toBeLessThanOrEqual(m.height);
      }
    }
  });

  it("edge geometry is finite — paths, chips, arrowheads", () => {
    for (const kind of ENGINE_KINDS) {
      for (const e of models[kind].edges) {
        expect(e.path).not.toMatch(/NaN|Infinity|undefined/);
        expect(Number.isFinite(e.chip.x) && Number.isFinite(e.chip.y)).toBe(true);
        expect(Number.isFinite(e.arrow.x) && Number.isFinite(e.arrow.y)).toBe(true);
      }
    }
  });

  it("nodes in the same row do not overlap vertically within a column", () => {
    for (const kind of ENGINE_KINDS) {
      const m = models[kind];
      const byLayer = new Map<number, typeof m.nodes>();
      for (const n of m.nodes) {
        byLayer.set(n.layer, [...(byLayer.get(n.layer) ?? []), n]);
      }
      for (const list of byLayer.values()) {
        const sorted = [...list].sort((a, b) => a.y - b.y);
        for (let i = 1; i < sorted.length; i++) {
          expect(sorted[i].y).toBeGreaterThanOrEqual(sorted[i - 1].y + sorted[i - 1].h);
        }
      }
    }
  });
});
