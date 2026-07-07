import { describe, expect, it } from "vitest";
import type { WorkflowSpec } from "@/lib/api";
import {
  specToYaml,
  specToToml,
  yamlToSpec,
  tomlToSpec,
  textToSpec,
  coerceSpec,
} from "./serialize";

const SPEC: WorkflowSpec = {
  version: 1,
  nodes: [
    { id: "n1", kind: "input", label: "Input", pos: { x: 64, y: 120 } },
    {
      id: "n2",
      kind: "engine",
      label: "Research",
      pos: { x: 240, y: 120 },
      config: { engine_def_id: "def-1", model: "sonnet" },
    },
    { id: "n3", kind: "gate", label: "Gate", pos: { x: 420, y: 120 } },
  ],
  edges: [
    { id: "e1", from: "n1", to: "n2" },
    { id: "e2", from: "n2", to: "n3", label: "ok" },
  ],
  inputs: ["query"],
  outputs: ["report"],
};

describe("workflow serialize", () => {
  it("round-trips through YAML", () => {
    const text = specToYaml(SPEC);
    const result = yamlToSpec(text);
    expect(result.errors).toEqual([]);
    expect(result.spec).toEqual(SPEC);
  });

  it("round-trips through TOML", () => {
    const text = specToToml(SPEC);
    const result = tomlToSpec(text);
    expect(result.errors).toEqual([]);
    expect(result.spec).toEqual(SPEC);
  });

  it("routes by file extension", () => {
    expect(textToSpec(specToToml(SPEC), "flow.toml").spec).toEqual(SPEC);
    expect(textToSpec(specToYaml(SPEC), "flow.yaml").spec).toEqual(SPEC);
  });

  it("reports YAML syntax errors", () => {
    const result = yamlToSpec("nodes: [unclosed");
    expect(result.spec).toBeNull();
    expect(result.errors.length).toBeGreaterThan(0);
  });

  it("rejects unknown top-level keys", () => {
    const result = coerceSpec({ version: 1, nodes: [], bogus: true });
    expect(result.spec).toBeNull();
    expect(result.errors.some((e) => e.includes("bogus"))).toBe(true);
  });

  it("rejects duplicate node ids and dangling edges", () => {
    const result = coerceSpec({
      version: 1,
      nodes: [
        { id: "a", kind: "input", label: "A", pos: { x: 0, y: 0 } },
        { id: "a", kind: "chat", label: "A2", pos: { x: 0, y: 0 } },
      ],
      edges: [{ id: "e1", from: "a", to: "missing" }],
    });
    expect(result.spec).toBeNull();
    expect(result.errors.some((e) => e.includes("duplicate"))).toBe(true);
    expect(result.errors.some((e) => e.includes("missing"))).toBe(true);
  });

  it("rejects invalid node kinds", () => {
    const result = coerceSpec({
      version: 1,
      nodes: [{ id: "a", kind: "teleport", label: "A", pos: { x: 0, y: 0 } }],
      edges: [],
    });
    expect(result.spec).toBeNull();
    expect(result.errors.some((e) => e.includes("kind"))).toBe(true);
  });

  it("fills defaults for missing optional fields", () => {
    const result = coerceSpec({
      version: 1,
      nodes: [{ id: "a", kind: "input", label: "A", pos: { x: 0, y: 0 } }],
    });
    expect(result.spec).not.toBeNull();
    expect(result.spec?.edges).toEqual([]);
    expect(result.spec?.inputs).toEqual([]);
    expect(result.spec?.outputs).toEqual([]);
  });

  it("round-trips an edge condition through YAML", () => {
    const spec: WorkflowSpec = {
      ...SPEC,
      edges: [SPEC.edges[0], { ...SPEC.edges[1], condition: 'verdict == "APPROVE"' }],
    };
    const text = specToYaml(spec);
    const result = yamlToSpec(text);
    expect(result.errors).toEqual([]);
    expect(result.spec).toEqual(spec);
  });

  it("round-trips an edge condition through TOML", () => {
    const spec: WorkflowSpec = {
      ...SPEC,
      edges: [SPEC.edges[0], { ...SPEC.edges[1], condition: 'verdict == "APPROVE"' }],
    };
    const text = specToToml(spec);
    const result = tomlToSpec(text);
    expect(result.errors).toEqual([]);
    expect(result.spec).toEqual(spec);
  });

  it("drops an empty or whitespace-only condition instead of emitting it", () => {
    const spec: WorkflowSpec = {
      ...SPEC,
      edges: [
        { ...SPEC.edges[0], condition: "" },
        { ...SPEC.edges[1], condition: "   " },
      ],
    };
    const yaml = specToYaml(spec);
    expect(yaml).not.toContain("condition");
    const yamlResult = yamlToSpec(yaml);
    expect(yamlResult.spec?.edges.every((e) => e.condition === undefined)).toBe(true);

    const toml = specToToml(spec);
    expect(toml).not.toContain("condition");
    const tomlResult = tomlToSpec(toml);
    expect(tomlResult.spec?.edges.every((e) => e.condition === undefined)).toBe(true);
  });
});
