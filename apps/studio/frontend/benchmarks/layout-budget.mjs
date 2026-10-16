// Run from apps/studio/frontend with Node 24 or newer:
// node benchmarks/layout-budget.mjs [--sweep|--chord-sweep|--fixtures]
// --fixtures times the committed module. --sweep/--chord-sweep time an in-memory
// copy of it with only the two admission limits substituted, so those samples
// describe a candidate, not the committed layout; --fixtures is the committed
// one. No dependency or project file is written. A separate untimed pass counts
// Dagre calls/dummies by wrapping both in process; no timing sample is taken
// while that wrapper is installed.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire, stripTypeScriptTypes } from "node:module";
import { cpus, platform, arch, release } from "node:os";
import { performance } from "node:perf_hooks";
import { getLayoutedElements } from "../src/components/canvas/useLayout.ts";

const require = createRequire(import.meta.url);
const dagre = require("dagre");
const normalize = require("dagre/lib/normalize");
const originalLayout = dagre.layout;
const originalNormalize = normalize.run;
const source = readFileSync(
  new URL("../src/components/canvas/useLayout.ts", import.meta.url),
  "utf8",
);
const configuredWork = Number(source.match(/const DAGRE_WORK_LIMIT = (\d+);/)[1]);
const configuredNodes = Number(source.match(/const DAGRE_NODE_LIMIT = (\d+);/)[1]);
const warmups = 1;
const samples = 7;
const selectionMedianMs = 20;
const candidates = [
  [96, 64],
  [144, 96],
  [192, 128],
  [240, 160],
];
const mode = process.argv[2];
assert(
  [undefined, "--sweep", "--chord-sweep", "--fixtures"].includes(mode),
  "Unknown benchmark mode",
);

const node = (id) => ({ id, position: { x: 0, y: 0 }, data: { label: id } });
const edge = (source, target, id = `${source}-${target}`) => ({ id, source, target });
function chain(count) {
  const nodes = Array.from({ length: count }, (_, index) => node(`c${index}`));
  return {
    nodes,
    edges: nodes.slice(1).map((entry, index) => edge(nodes[index].id, entry.id)),
  };
}
function fanIn(roots, depth) {
  const graph = chain(depth);
  const early = Array.from({ length: roots }, (_, index) => node(`early-${index}`));
  return {
    nodes: [...graph.nodes, ...early, node("join")],
    edges: [
      ...graph.edges,
      edge(`c${depth - 1}`, "join"),
      ...early.map((entry) => edge(entry.id, "join")),
    ],
  };
}
function fanOut(count) {
  const leaves = Array.from({ length: count }, (_, index) => node(`leaf-${index}`));
  return {
    nodes: [node("root"), ...leaves],
    edges: leaves.map((entry) => edge("root", entry.id)),
  };
}
function layers(width, count) {
  const groups = Array.from({ length: count }, (_, rank) =>
    Array.from({ length: width }, (_, index) => node(`r${rank}-${index}`)),
  );
  return {
    nodes: groups.flat(),
    edges: groups
      .slice(1)
      .flatMap((group, index) =>
        groups[index].flatMap((source) => group.map((target) => edge(source.id, target.id))),
      ),
  };
}
function cyclic(count, backEdges) {
  const graph = chain(count);
  graph.edges.push(
    ...graph.nodes.slice(0, backEdges).map((entry) => edge(`c${count - 1}`, entry.id)),
  );
  return graph;
}

// Independent accounting from the returned rank map. Parallel source/target
// pairs consume one graphlib edge; original edge records remain in the output.
function workOf(graph, ranks) {
  const seen = new Map();
  let work = graph.nodes.length;
  let nonForward = false;
  let pairs = 0;
  for (const entry of graph.edges) {
    const a = ranks.get(entry.source);
    const b = ranks.get(entry.target);
    if (a === undefined || b === undefined) continue;
    if (entry.source !== entry.target && b <= a) nonForward = true;
    const targets = seen.get(entry.source) ?? new Set();
    if (targets.has(entry.target)) continue;
    targets.add(entry.target);
    seen.set(entry.source, targets);
    work += 2 * Math.max(1, b - a);
    pairs++;
  }
  return { work, pairs, nonForward };
}

function median(values) {
  return [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
}
function measure(name, graph, layout, limits, extra = {}) {
  assert.equal(dagre.layout, originalLayout);
  assert.equal(normalize.run, originalNormalize);
  for (let index = 0; index < warmups; index++) layout(graph.nodes, graph.edges);
  const timings = [];
  let result;
  for (let index = 0; index < samples; index++) {
    const started = performance.now();
    result = layout(graph.nodes, graph.edges);
    timings.push(performance.now() - started);
  }
  let calls = 0;
  let dummies = 0;
  try {
    dagre.layout = function (...args) {
      calls++;
      return originalLayout.apply(this, args);
    };
    normalize.run = function (graph) {
      originalNormalize(graph);
      dummies += graph
        .nodes()
        .filter((id) => ["edge", "edge-label"].includes(graph.node(id).dummy)).length;
    };
    layout(graph.nodes, graph.edges);
  } finally {
    dagre.layout = originalLayout;
    normalize.run = originalNormalize;
  }
  const accounting = workOf(graph, result.ranks);
  const expectedAdmission =
    graph.nodes.length <= limits.nodes && accounting.work <= limits.work && !accounting.nonForward;
  assert.equal(calls, Number(expectedAdmission), `${name}: admission differs from accounting`);
  assert.equal(result.nodes.length, graph.nodes.length);
  assert.equal(result.edges.length, graph.edges.length);
  if (calls) assert(graph.nodes.length + dummies <= accounting.work, `${name}: dummy bound`);
  const rankGroups = new Map();
  for (const entry of result.nodes) {
    const rank = result.ranks.get(entry.id);
    const xs = rankGroups.get(rank) ?? new Set();
    xs.add(entry.position.x);
    rankGroups.set(rank, xs);
  }
  const record = {
    name,
    limits,
    nodes: graph.nodes.length,
    edges: graph.edges.length,
    ...accounting,
    dagreCalls: calls,
    edgeDummies: dummies,
    medianMs: median(timings),
    maximumMs: Math.max(...timings),
    samplesMs: timings,
    width: result.width,
    height: result.height,
    continuationEdges: result.edges.filter((entry) => entry.data?.continuation).length,
    maxColumnsPerRank: Math.max(0, ...[...rankGroups.values()].map((xs) => xs.size)),
    ...extra,
  };
  console.log(JSON.stringify(record));
  return record;
}

// Each candidate runs the same complete pipeline with only the two admission
// constants changed in memory. No project or dependency file is rewritten.
async function variant(work, nodes) {
  let text = source
    .replace(/const DAGRE_WORK_LIMIT = \d+;/, `const DAGRE_WORK_LIMIT = ${work};`)
    .replace(/const DAGRE_NODE_LIMIT = \d+;/, `const DAGRE_NODE_LIMIT = ${nodes};`);
  text = stripTypeScriptTypes(text)
    .replace('from "react"', `from ${JSON.stringify(import.meta.resolve("react"))}`)
    .replace('from "dagre"', `from ${JSON.stringify(import.meta.resolve("dagre"))}`);
  const module = await import(
    `data:text/javascript;base64,${Buffer.from(text).toString("base64")}`
  );
  return module.getLayoutedElements;
}
function padToWork(graph, work, layout) {
  const result = layout(graph.nodes, graph.edges);
  const remaining = work - workOf(graph, result.ranks).work;
  assert(remaining >= 0);
  graph.nodes.push(...Array.from({ length: remaining }, (_, index) => node(`pad-${index}`)));
  return graph;
}
function boundaryFamilies(work, cap, layout) {
  const chainLength = Math.floor((work + 2) / 3);
  const chord = chain(Math.floor((work + 4) / 5));
  chord.edges.push(edge("c0", chord.nodes.at(-1).id));
  const linked = chain(Math.floor((work - cap) / 2) + 1);
  linked.nodes.push(
    ...Array.from({ length: cap - linked.nodes.length }, (_, i) => node(`solo-${i}`)),
  );
  const width = Math.floor((Math.sqrt(1 + 2 * work) - 1) / 2);
  let layerWidth = 1;
  while (4 * (layerWidth + 1) ** 2 + 3 * (layerWidth + 1) <= work) layerWidth++;
  let bestFanIn;
  let mostDummies = -1;
  for (let depth = 2; depth <= 64; depth++) {
    for (let roots = 2; roots <= 16; roots++) {
      const count = depth + roots + 1;
      const spans = depth * (roots + 1);
      const cost = count + 2 * spans;
      const dummies = 2 * spans - (depth + roots);
      if (cost <= work && count <= cap && dummies > mostDummies) {
        bestFanIn = fanIn(roots, depth);
        mostDummies = dummies;
      }
    }
  }
  return [
    ["chain-at-work", padToWork(chain(chainLength), work, layout)],
    ["chain-with-long-chord", padToWork(chord, work, layout)],
    [
      "isolated-at-node-cap",
      { nodes: Array.from({ length: cap }, (_, i) => node(`solo-${i}`)), edges: [] },
    ],
    ["node-cap-plus-chain-at-work", padToWork(linked, work, layout)],
    ["fan-out-at-work", padToWork(fanOut(Math.floor((work - 1) / 3)), work, layout)],
    ["fan-in-most-dummies", padToWork(bestFanIn, work, layout)],
    ["complete-two-layer", padToWork(layers(width, 2), work, layout)],
    ["complete-three-layer", padToWork(layers(layerWidth, 3), work, layout)],
  ];
}

console.log(
  JSON.stringify({
    kind: "environment",
    node: process.version,
    platform: platform(),
    arch: arch(),
    release: release(),
    cpu: cpus()[0]?.model,
    sourceSha256: createHash("sha256").update(source).digest("hex"),
    dagre: require("dagre/package.json").version,
    configured: { work: configuredWork, nodes: configuredNodes },
    warmups,
    samples,
    timing: "uninstrumented full pipeline; dummy/call inspection is a separate untimed run",
    selection: `largest listed candidate with every representative boundary-family median <= ${selectionMedianMs}ms on this host; no universal timing bound`,
    mode: mode ?? "all",
  }),
);

if (mode !== "--fixtures") {
  const choices = [];
  for (const [work, nodes] of candidates) {
    const layout = await variant(work, nodes);
    const records = boundaryFamilies(work, nodes, layout)
      .filter(([name]) => mode !== "--chord-sweep" || name === "chain-with-long-chord")
      .map(([name, graph]) =>
        measure(
          `sweep-${work}-${nodes}-${name}`,
          graph,
          layout,
          { work, nodes },
          { phase: "sweep" },
        ),
      );
    assert(
      records.every((record) => record.dagreCalls === 1),
      "Sweep family must be admitted",
    );
    choices.push({
      work,
      nodes,
      largestMedianMs: Math.max(...records.map((record) => record.medianMs)),
      largestMaximumMs: Math.max(...records.map((record) => record.maximumMs)),
      qualifies: records.every((record) => record.medianMs <= selectionMedianMs),
    });
  }
  console.log(
    JSON.stringify({
      kind: "selection",
      choices,
      recommended: choices.filter((c) => c.qualifies).at(-1) ?? null,
    }),
  );
}
if (mode !== "--sweep" && mode !== "--chord-sweep") {
  const parallel = {
    nodes: [node("a"), node("b")],
    edges: Array.from({ length: 100 }, (_, index) => edge("a", "b", `parallel-${index}`)),
  };
  const limits = { work: configuredWork, nodes: configuredNodes };
  const chainLength = Math.floor((configuredWork + 2) / 3);
  const longChord = chain(39);
  longChord.edges.push(edge("c0", "c38"));
  const fixtures = [
    ["fan-in-100-roots-depth10", fanIn(100, 10)],
    ["cycle-N40-back0-through36", cyclic(40, 37)],
    ["cycle-N46-back0-through27", cyclic(46, 28)],
    ["chain39-with-long-chord", longChord],
    ["fan-in5-depth9", fanIn(5, 9)],
    ["parallel-100-A-to-B", parallel],
    ["wrap-seven-siblings", fanOut(7)],
    ["wrap-eight-siblings", fanOut(8)],
    ["fold-chain-five", chain(5)],
    ["fold-chain-six", chain(6)],
    ["fold-at-work-bound", padToWork(chain(chainLength), configuredWork, getLayoutedElements)],
    ["fold-over-work-bound", chain(chainLength + 1)],
    [
      "over-node-cap",
      {
        nodes: Array.from({ length: configuredNodes + 1 }, (_, i) => node(`solo-${i}`)),
        edges: [],
      },
    ],
    ...boundaryFamilies(configuredWork, configuredNodes, getLayoutedElements),
  ];
  for (const [name, graph] of fixtures)
    measure(name, graph, getLayoutedElements, limits, { phase: "fixtures" });
}
