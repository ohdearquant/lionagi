/**
 * Engine topology catalog — the canonical description of what each engine
 * kind in `lionagi/engines/` actually executes (DESIGN-SYSTEM §12: the canvas
 * renders this, nothing else).
 *
 * Every stage, emission name, condition, and bound below is transcribed from
 * the engine sources (sourceRef on each entry). Stage ids, roles, emission
 * class names, and condition strings are code identities — they render in
 * mono and are not translated. If an engine source changes, this catalog is
 * the single place to update.
 */

export type EngineKind = "research" | "review" | "coding" | "hypothesis" | "planning";

export const ENGINE_KINDS: EngineKind[] = [
  "research",
  "review",
  "coding",
  "hypothesis",
  "planning",
];

/** The fields the backend actually persists for an engine definition. */
export interface EngineDefFields {
  name: string;
  kind: EngineKind;
  model?: string | null;
  max_depth?: number | null;
  max_agents?: number | null;
  options?: { test_cmd?: string; export_dir?: string } | null;
  description?: string | null;
}

export interface TopologyStage {
  /** Stable id — node identity and layout anchor. */
  id: string;
  /** Short display name (code identity, stays English). */
  label: string;
  /** Casts role driving the stage; null for non-agent stages. */
  role: string | null;
  kind: "agent" | "team" | "tool" | "synth" | "input";
  /**
   * Key into the engine's per-stage `models={...}` routing; null means the
   * stage resolves to the engine's base `model`. Rendered on the node as the
   * resolved model chip.
   */
  modelStage: string | null;
  /** Real emission class names this stage is granted (from the source). */
  emits: string[];
  /**
   * Node-type tag shown in the card header. Defaults to the kind (entry /
   * agent / team / tool / synth); set explicitly where the runtime shape is
   * richer, e.g. planning's workers node is a self-expanding reactive DAG.
   */
  typeLabel?: string;
  /** Fan-out multiplicity annotation, e.g. "× dimension". */
  perItem?: string;
  /** Fixed cognitive-mode overlay(s) from the source. */
  mode?: string;
  /** Tools / sandbox / guard facts worth showing on the node face. */
  note?: string;
  /** Runs even after the budget is exhausted (terminal stages). */
  exempt?: boolean;
  /**
   * Spawn-unit membership: stages sharing a group id run as one sequential
   * team per spawned unit (e.g. research's exploration node). Reactions that
   * re-enter the unit observe at the group boundary, not a member stage.
   */
  group?: string;
}

export interface TopologyEdge {
  from: string;
  to: string;
  /**
   * seq — direct pipeline hand-off inside `_run()`.
   * reaction — `run.observe(<on>) → run.spawn(...)`.
   * loop — reaction that re-enters an earlier stage (recursion / fix loop).
   */
  kind: "seq" | "reaction" | "loop";
  /** Emission class that triggers a reaction/loop edge. */
  on?: string;
  /** Trigger condition, transcribed from the source (mono). */
  condition?: string;
  /** What bounds the edge: budget, depth, dedup, rounds (mono). */
  bound?: string;
  /** Passes through the quality judge when `judge_model` is configured. */
  judgeGated?: boolean;
}

export interface EngineTopology {
  kind: EngineKind;
  /** The pipeline's shape, named as the engine docstrings name it. */
  shape: "tree" | "fanout" | "chain" | "cascade" | "dag";
  stages: TopologyStage[];
  edges: TopologyEdge[];
  /** Labels for spawn-unit groups referenced by stage.group. */
  groups?: Record<string, { label: string }>;
  /**
   * Engine-internal defaults that shape the run but are NOT persisted by
   * engine_defs — rendered read-only so the diagram stays truthful without
   * pretending they are saved. Keys are knob names from the engine __init__.
   */
  defaults: Record<string, string>;
  /** Whether the persisted max_depth knob applies, and what it means here. */
  maxDepth: { applies: boolean; meaning?: string };
  /** Whether options.test_cmd applies (required for coding). */
  testCmd: { applies: boolean; required?: boolean };
  /** Source of truth for this topology. */
  sourceRef: string;
}

export const ENGINE_TOPOLOGIES: Record<EngineKind, EngineTopology> = {
  // ── research — recursive tree ────────────────────────────────────────────
  // A team of casts roles explores a topic; a sharp ContradictionFound (or an
  // explicit DepthRequested) spawns a deeper exploration node, judge-gated
  // and bounded by depth + topic dedup. Findings feed synthesis only.
  // Quiescence → synthesis.
  research: {
    kind: "research",
    shape: "tree",
    groups: { exploration: { label: "exploration node" } },
    stages: [
      { id: "topic", label: "topic", role: null, kind: "input", modelStage: null, emits: [] },
      {
        id: "researcher",
        label: "researcher",
        role: "researcher",
        kind: "agent",
        modelStage: "researcher",
        emits: ["FindingEmitted", "DepthRequested"],
        perItem: "× exploration node",
        group: "exploration",
      },
      {
        id: "analyst",
        label: "analyst",
        role: "analyst",
        kind: "agent",
        modelStage: "analyst",
        emits: ["FindingEmitted", "ContradictionFound"],
        perItem: "× exploration node",
        group: "exploration",
      },
      {
        id: "critic",
        label: "critic",
        role: "critic",
        kind: "agent",
        modelStage: "critic",
        emits: ["DepthRequested"],
        perItem: "× exploration node",
        group: "exploration",
      },
      {
        id: "synthesize",
        label: "synthesize",
        role: "synthesizer",
        kind: "synth",
        modelStage: "synthesize",
        emits: [],
        exempt: true,
        note: "reads every finding from the emission store",
      },
    ],
    edges: [
      { from: "topic", to: "researcher", kind: "seq" },
      { from: "researcher", to: "analyst", kind: "seq" },
      { from: "analyst", to: "critic", kind: "seq" },
      {
        from: "analyst",
        to: "researcher",
        kind: "loop",
        on: "ContradictionFound",
        condition: "severity > 0.7 ∧ depth < max_depth",
        bound: "max_depth · dedup(topic) · max_agents",
        judgeGated: true,
      },
      {
        from: "critic",
        to: "researcher",
        kind: "loop",
        on: "DepthRequested",
        condition: "parent_depth + 1 ≤ max_depth",
        bound: "max_depth · dedup(topic) · max_agents",
        judgeGated: true,
      },
      { from: "critic", to: "synthesize", kind: "seq", condition: "on quiescence" },
    ],
    defaults: {
      roles: "researcher → analyst → critic (sequential team per node)",
      severity_threshold: "0.7",
      repair_retries: "1 (per stage + node-level backstop)",
      partial_export: "budget exhaustion still synthesizes collected findings",
    },
    maxDepth: { applies: true, meaning: "recursion depth of the exploration tree" },
    testCmd: { applies: false },
    sourceRef: "lionagi/engines/research.py",
  },

  // ── review — dimensional fan-out ─────────────────────────────────────────
  // One reviewer per dimension in parallel; a critical/major issue reactively
  // spawns an adversarial verifier that tries to refute it. Quiescence → one
  // ReviewVerdict.
  review: {
    kind: "review",
    shape: "fanout",
    stages: [
      { id: "artifact", label: "artifact", role: null, kind: "input", modelStage: null, emits: [] },
      {
        id: "review",
        label: "review",
        role: "critic",
        kind: "agent",
        modelStage: "review",
        emits: ["IssueFound"],
        perItem: "× dimension (parallel)",
        mode: "per-dimension: systematic · adversarial · evidential · metacognitive",
      },
      {
        id: "verify",
        label: "verify",
        role: "critic",
        kind: "agent",
        modelStage: "verify",
        emits: ["VerifyResult"],
        mode: "adversarial",
        note: "tries to REFUTE the issue",
      },
      {
        id: "verdict",
        label: "verdict",
        role: "synthesizer",
        kind: "synth",
        modelStage: "verdict",
        emits: ["ReviewVerdict"],
        exempt: true,
        note: "weighs refuted issues down · APPROVE / FIXES / CHANGES / REJECT",
      },
    ],
    edges: [
      { from: "artifact", to: "review", kind: "seq" },
      {
        from: "review",
        to: "verify",
        kind: "reaction",
        on: "IssueFound",
        condition: "severity ∈ {critical, major}",
        bound: "dedup(description) · max_agents",
      },
      { from: "review", to: "verdict", kind: "seq", condition: "all issues" },
      { from: "verify", to: "verdict", kind: "seq", condition: "on quiescence" },
    ],
    defaults: {
      dimensions: "correctness · security · performance · maintainability",
      verify_severities: "critical, major",
      reviewer_role: "critic — verifier_role: critic — synthesis_role: synthesizer",
      repair_retries: "1",
    },
    maxDepth: { applies: false },
    testCmd: { applies: false },
    sourceRef: "lionagi/engines/review.py",
  },

  // ── coding — gated chain with fix loop ───────────────────────────────────
  // plan → implement (with tools) → test subprocess → fix loop on failure →
  // verify diff → conclude. The test runner is NOT an agent.
  coding: {
    kind: "coding",
    shape: "chain",
    stages: [
      { id: "spec", label: "spec", role: null, kind: "input", modelStage: null, emits: [] },
      {
        id: "plan",
        label: "plan",
        role: "analyst",
        kind: "agent",
        modelStage: "plan",
        emits: ["WorkPlanned"],
        note: "no tools",
      },
      {
        id: "implement",
        label: "implement",
        role: "implementer",
        kind: "agent",
        modelStage: "implement",
        emits: ["ChangeProposed"],
        note: "coding toolkit · path-guarded · destructive-cmd guard · permissions: safe",
      },
      {
        id: "test",
        label: "test",
        role: null,
        kind: "tool",
        modelStage: null,
        emits: ["TestsRan"],
        note: "subprocess: options.test_cmd · 600s timeout · not an agent",
      },
      {
        id: "verify",
        label: "verify",
        role: "critic",
        kind: "agent",
        modelStage: "verify",
        emits: ["VerifyResult"],
        note: "reviews the diff against acceptance",
      },
      {
        id: "conclude",
        label: "conclude",
        role: null,
        kind: "synth",
        modelStage: null,
        emits: ["CodeResultRecorded"],
        exempt: true,
        note: "captures diff + writes report",
      },
    ],
    edges: [
      { from: "spec", to: "plan", kind: "seq" },
      { from: "plan", to: "implement", kind: "seq", on: "WorkPlanned" },
      { from: "implement", to: "test", kind: "seq", on: "ChangeProposed" },
      {
        from: "test",
        to: "implement",
        kind: "loop",
        on: "TestsRan",
        condition: "passed = false",
        bound: "max_fix_rounds = 3",
        judgeGated: true,
      },
      { from: "test", to: "verify", kind: "seq", condition: "passed = true" },
      { from: "verify", to: "conclude", kind: "seq", on: "VerifyResult" },
    ],
    defaults: {
      roles: "plan: analyst — implement: implementer — verify: critic",
      max_fix_rounds: "3",
      test_timeout_s: "600",
      repair_retries: "1",
    },
    maxDepth: { applies: false },
    testCmd: { applies: true, required: true },
    sourceRef: "lionagi/engines/coding.py",
  },

  // ── hypothesis — evidence cascade ────────────────────────────────────────
  // Every hop is a reaction (observe → spawn): finding → question → evidence
  // → hypothesis → experiment → result → conclusion → application. Cycles are
  // generation-bounded by max_depth; quiescence → evidence report.
  hypothesis: {
    kind: "hypothesis",
    shape: "cascade",
    stages: [
      {
        id: "seed",
        label: "seed findings",
        role: null,
        kind: "input",
        modelStage: null,
        emits: ["FindingPosted"],
      },
      {
        id: "extract",
        label: "extract",
        role: "analyst",
        kind: "agent",
        modelStage: "extract",
        emits: ["QuestionRaised"],
        note: "≤ 8 questions per finding (soft cap)",
      },
      {
        id: "research",
        label: "research",
        role: "researcher",
        kind: "agent",
        modelStage: "research",
        emits: ["EvidenceCollected"],
      },
      {
        id: "hypothesize",
        label: "hypothesize",
        role: "analyst",
        kind: "agent",
        modelStage: "hypothesize",
        emits: ["HypothesisFormed"],
        note: "falsifiable prediction from the evidence",
      },
      {
        id: "design",
        label: "design",
        role: "evaluator",
        kind: "agent",
        modelStage: "design",
        emits: ["ExperimentDesigned"],
      },
      {
        id: "validate",
        label: "validate",
        role: "analyst",
        kind: "agent",
        modelStage: "validate",
        emits: ["ResultRecorded"],
        note: "methods ∈ {analysis, comparison, proof} run inline; others queue pending",
      },
      {
        id: "conclude",
        label: "conclude",
        role: "critic",
        kind: "agent",
        modelStage: "conclude",
        emits: ["ConclusionDrawn"],
      },
      {
        id: "apply",
        label: "apply",
        role: "architect",
        kind: "agent",
        modelStage: "apply",
        emits: ["ApplicationMapped"],
        note: "maps conclusions onto the decision register",
      },
      {
        id: "synthesize",
        label: "synthesize",
        role: "synthesizer",
        kind: "synth",
        modelStage: "synthesize",
        emits: [],
        exempt: true,
        note: "evidence report · chains.json + report.md export",
      },
    ],
    edges: [
      {
        from: "seed",
        to: "extract",
        kind: "reaction",
        on: "FindingPosted",
        condition: "gen ≤ max_depth",
        bound: "dedup(description)",
      },
      {
        from: "extract",
        to: "research",
        kind: "reaction",
        on: "QuestionRaised",
        condition: "gen ≤ max_depth",
        bound: "dedup(question)",
        judgeGated: true,
      },
      { from: "research", to: "hypothesize", kind: "seq", on: "EvidenceCollected" },
      {
        from: "hypothesize",
        to: "design",
        kind: "reaction",
        on: "HypothesisFormed",
        bound: "dedup(statement)",
      },
      {
        from: "design",
        to: "validate",
        kind: "reaction",
        on: "ExperimentDesigned",
        condition: "method executable",
      },
      { from: "validate", to: "conclude", kind: "reaction", on: "ResultRecorded" },
      { from: "conclude", to: "apply", kind: "reaction", on: "ConclusionDrawn" },
      {
        from: "apply",
        to: "extract",
        kind: "loop",
        on: "FindingPosted",
        condition: "follow-up findings re-enter the cascade",
        bound: "gen ≤ max_depth",
      },
      { from: "apply", to: "synthesize", kind: "seq", condition: "on quiescence" },
    ],
    defaults: {
      roles:
        "extract: analyst — research: researcher — hypothesize: analyst — design: evaluator — validate: analyst — conclude: critic — apply: architect",
      executable_methods: "analysis · comparison · proof (benchmark queues pending)",
      max_questions: "8",
      repair_retries: "1",
      partial_export: "budget exhaustion still writes the evidence report",
    },
    maxDepth: { applies: true, meaning: "cycle generations before follow-ups are capped" },
    testCmd: { applies: false },
    sourceRef: "lionagi/engines/hypothesis.py",
  },

  // ── planning — plan → reactive DAG → synthesize ──────────────────────────
  // An orchestrator decomposes the prompt into TaskAssignments; assignments
  // wire a dependency DAG of role workers; reactive workers may grow the live
  // DAG via SpawnRequest. The engine `li o flow` fronts.
  planning: {
    kind: "planning",
    shape: "dag",
    stages: [
      { id: "prompt", label: "prompt", role: null, kind: "input", modelStage: null, emits: [] },
      {
        id: "orchestrate",
        label: "orchestrate",
        role: "orchestrator",
        kind: "agent",
        modelStage: null,
        emits: ["TaskAssignment"],
        note: "decomposes into a dependency DAG · one reinforced retry, then fail loud",
      },
      {
        id: "workers",
        label: "workers",
        role: null,
        kind: "team",
        modelStage: null,
        emits: ["SpawnRequest"],
        typeLabel: "reactive DAG",
        perItem: "× assignee (planned per run)",
        note: "self-expanding worker DAG — observes TaskAssignment, may grow itself via SpawnRequest · roster: researcher · analyst · critic · architect · synthesizer",
      },
      {
        id: "synthesize",
        label: "synthesize",
        role: "synthesizer",
        kind: "synth",
        modelStage: "synthesize",
        emits: [],
        exempt: true,
        note: "reconciles worker outputs into one deliverable",
      },
    ],
    edges: [
      { from: "prompt", to: "orchestrate", kind: "seq" },
      {
        from: "orchestrate",
        to: "workers",
        kind: "seq",
        on: "TaskAssignment",
        condition: "build_dag_graph wires dependencies",
      },
      {
        from: "workers",
        to: "workers",
        kind: "loop",
        on: "SpawnRequest",
        condition: "reactive: the live DAG self-expands",
        bound: "max_spawn = 50 · max_agents",
      },
      { from: "workers", to: "synthesize", kind: "seq", condition: "on quiescence" },
    ],
    defaults: {
      orchestrator_role: "orchestrator",
      roles: "researcher · analyst · critic · architect · synthesizer",
      reactive: "true (workers granted SpawnRequest)",
    },
    maxDepth: { applies: false },
    testCmd: { applies: false },
    sourceRef: "lionagi/engines/planning.py",
  },
};

/**
 * Resolve the model chip for a stage: per-stage override from the engine's
 * `models={...}` routing is not persisted by engine_defs, so in the designer
 * the resolution is `def.model ?? engine default`. Kept as a helper so the
 * node face and the config pane render the same answer.
 */
export function resolveStageModel(defModel: string | null | undefined): string {
  return defModel?.trim() || "provider default";
}
