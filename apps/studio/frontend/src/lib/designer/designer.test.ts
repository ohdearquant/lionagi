/**
 * Designer library tests — topology invariants and def request body builder.
 * No serializer/spec/validator imports (those files are deleted).
 */
import { describe, it, expect } from "vitest";
import { ENGINE_KINDS, ENGINE_TOPOLOGIES } from "./topology";
import { buildDefBody } from "./draft";
import type { EngineDefDraft } from "./draft";

// ─── Topology invariants ──────────────────────────────────────────────────────

describe("topology catalog — all 5 kinds present", () => {
  it("ENGINE_KINDS covers exactly 5 kinds", () => {
    expect(ENGINE_KINDS).toHaveLength(5);
    expect(ENGINE_KINDS).toContain("research");
    expect(ENGINE_KINDS).toContain("review");
    expect(ENGINE_KINDS).toContain("coding");
    expect(ENGINE_KINDS).toContain("hypothesis");
    expect(ENGINE_KINDS).toContain("planning");
  });

  it("ENGINE_TOPOLOGIES has an entry for every kind", () => {
    for (const kind of ENGINE_KINDS) {
      expect(ENGINE_TOPOLOGIES[kind]).toBeDefined();
      expect(ENGINE_TOPOLOGIES[kind].kind).toBe(kind);
    }
  });
});

describe("topology invariants — edge endpoints are defined stages", () => {
  for (const kind of ENGINE_KINDS) {
    it(`${kind}: every edge endpoint ∈ stages`, () => {
      const topo = ENGINE_TOPOLOGIES[kind];
      const stageIds = new Set(topo.stages.map((s) => s.id));
      for (const edge of topo.edges) {
        expect(stageIds.has(edge.from), `edge.from=${edge.from} not in stages`).toBe(true);
        expect(stageIds.has(edge.to), `edge.to=${edge.to} not in stages`).toBe(true);
      }
    });
  }
});

describe("topology invariants — emission names non-empty", () => {
  for (const kind of ENGINE_KINDS) {
    it(`${kind}: every emits entry is a non-empty string`, () => {
      const topo = ENGINE_TOPOLOGIES[kind];
      for (const stage of topo.stages) {
        for (const e of stage.emits) {
          expect(typeof e).toBe("string");
          expect(e.trim().length).toBeGreaterThan(0);
        }
      }
    });
  }
});

describe("topology invariants — coding requires testCmd flag", () => {
  it("coding: testCmd.applies = true, required = true", () => {
    const topo = ENGINE_TOPOLOGIES["coding"];
    expect(topo.testCmd.applies).toBe(true);
    expect(topo.testCmd.required).toBe(true);
  });

  it("non-coding kinds: testCmd.applies = false", () => {
    for (const kind of ENGINE_KINDS) {
      if (kind === "coding") continue;
      expect(ENGINE_TOPOLOGIES[kind].testCmd.applies).toBe(false);
    }
  });
});

describe("topology invariants — maxDepth.applies consistency", () => {
  it("research and hypothesis: maxDepth.applies = true with meaning", () => {
    expect(ENGINE_TOPOLOGIES["research"].maxDepth.applies).toBe(true);
    expect(ENGINE_TOPOLOGIES["research"].maxDepth.meaning).toBeTruthy();
    expect(ENGINE_TOPOLOGIES["hypothesis"].maxDepth.applies).toBe(true);
    expect(ENGINE_TOPOLOGIES["hypothesis"].maxDepth.meaning).toBeTruthy();
  });

  it("review, coding, planning: maxDepth.applies = false", () => {
    for (const kind of ["review", "coding", "planning"] as const) {
      expect(ENGINE_TOPOLOGIES[kind].maxDepth.applies).toBe(false);
    }
  });
});

describe("topology invariants — sourceRef present", () => {
  for (const kind of ENGINE_KINDS) {
    it(`${kind}: sourceRef is a non-empty string`, () => {
      expect(ENGINE_TOPOLOGIES[kind].sourceRef.trim().length).toBeGreaterThan(0);
    });
  }
});

// ─── Def request body builder ────────────────────────────────────────────────

function makeDraft(overrides: Partial<EngineDefDraft> = {}): EngineDefDraft {
  return {
    name: "my-engine",
    kind: "research",
    model: "",
    max_agents: "",
    max_depth: "",
    test_cmd: "",
    export_dir: "",
    description: "",
    stages: {},
    ...overrides,
  };
}

describe("buildDefBody", () => {
  it("minimal draft produces name + kind only", () => {
    const body = buildDefBody(makeDraft());
    expect(body.name).toBe("my-engine");
    expect(body.kind).toBe("research");
    expect(body.model).toBeUndefined();
    expect(body.description).toBeUndefined();
    expect(body.max_agents).toBeUndefined();
    expect(body.max_depth).toBeUndefined();
    expect(body.options).toBeUndefined();
  });

  it("model included when non-blank", () => {
    const body = buildDefBody(makeDraft({ model: "claude_code/sonnet" }));
    expect(body.model).toBe("claude_code/sonnet");
  });

  it("max_agents parsed to number, dropped when invalid", () => {
    expect(buildDefBody(makeDraft({ max_agents: "10" })).max_agents).toBe(10);
    expect(buildDefBody(makeDraft({ max_agents: "" })).max_agents).toBeUndefined();
    expect(buildDefBody(makeDraft({ max_agents: "abc" })).max_agents).toBeUndefined();
    expect(buildDefBody(makeDraft({ max_agents: "0" })).max_agents).toBeUndefined();
    expect(buildDefBody(makeDraft({ max_agents: "101" })).max_agents).toBeUndefined();
  });

  it("max_depth parsed to number, dropped when invalid", () => {
    expect(buildDefBody(makeDraft({ max_depth: "5" })).max_depth).toBe(5);
    expect(buildDefBody(makeDraft({ max_depth: "" })).max_depth).toBeUndefined();
  });

  it("options built from test_cmd + export_dir, dropped when both blank", () => {
    expect(buildDefBody(makeDraft({ test_cmd: "pytest tests/" })).options).toEqual({
      test_cmd: "pytest tests/",
    });
    expect(buildDefBody(makeDraft({ test_cmd: "pytest", export_dir: "./out" })).options).toEqual({
      test_cmd: "pytest",
      export_dir: "./out",
    });
    expect(buildDefBody(makeDraft()).options).toBeUndefined();
  });

  it("description included when non-blank", () => {
    expect(buildDefBody(makeDraft({ description: "A good engine" })).description).toBe(
      "A good engine",
    );
    expect(buildDefBody(makeDraft({ description: "  " })).description).toBeUndefined();
  });

  it("coding kind in draft sets kind field correctly", () => {
    const body = buildDefBody(makeDraft({ kind: "coding", test_cmd: "npm test" }));
    expect(body.kind).toBe("coding");
    expect((body.options as Record<string, string>)?.test_cmd).toBe("npm test");
  });

  it("stages always sent — empty map clears overrides on update", () => {
    expect(buildDefBody(makeDraft()).stages).toEqual({});
  });

  it("stage overrides trimmed; blank entries dropped", () => {
    const body = buildDefBody(
      makeDraft({
        stages: {
          analyst: { role: " evaluator ", model: "" },
          synthesize: { model: "claude_code/sonnet" },
          researcher: { role: "  ", model: "" },
        },
      }),
    );
    expect(body.stages).toEqual({
      analyst: { role: "evaluator" },
      synthesize: { model: "claude_code/sonnet" },
    });
  });
});
