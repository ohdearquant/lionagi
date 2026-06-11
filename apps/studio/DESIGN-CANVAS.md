# The Canvas — Event-Driven Workflow Design

**Companion to DESIGN.md §Designer.** This document is grounded in a read of the
actual substrate (casts/emission.py, casts/pattern.py, operations/_observe.py,
session/observer.py, session/signal.py, engines/engine.py) — not the webapp POC.

---

## 1. The problem, precisely

lionagi has three orchestration styles, and only the weakest one is visible today:

| Style | Substrate | Graph exists… | Visualizable today? |
|---|---|---|---|
| Static DAG | playbooks, `li o flow`, OperationGraphBuilder | at design time | yes (POC does read-only render) |
| Reactive DAG | `flow(reactive=True)` + SpawnRequest emissions | grows at run time | partially (nodes appear, but *why* is invisible) |
| Engine | Engine/EngineRun: `observe(type) → reaction`, budgets, judge gates | **never** — emergent from event→reaction chains | no |

Event-driven design is hard for exactly this reason: **a pub-sub edge is not a
thing, it's a coincidence** — an emitter and an observer are only connected at
the moment a signal flows through the bus. There is nothing to look at while
designing, and during a run the causality (*this Finding spawned that
investigator*) is buried in logs.

The canvas's job: **make the signal plane first-class geometry**, at design time
(potential topology) and run time (actual causality).

## 2. Canvas grammar — three elements

### Work nodes (things that execute)
An agent step, fanout group, or engine stage. Shows: role glyph + name, model
chip, and — the important part — **emission ports**: one small typed port per
capability in the node's grant (`Role.emits` ∪ explicit grant). A node with
`emits=(Finding, Gap)` has two visible output ports besides its data output.
The grant is *visible surface area*: you can see at a glance what an agent is
allowed to say to the system. (`EscalationRequest` is implicit on every
emitting node — rendered as a small red port on the node's corner, always.)

### Signal rails (typed channels)
When any reaction taps an emission type, that type gets a **rail** — a thin
vertical channel at the canvas edge (or routed between node clusters), colored
by emission category. Emitter ports wire INTO the rail; reactions tap OUT of
it. The rail is the bus made visible: it renders the decoupling honestly
(emitters don't know their observers) instead of faking point-to-point edges.

Emission categories → rail colors (from emission.py's own taxonomy):

| Category | Types | Color |
|---|---|---|
| discovery | Finding, Conflict, Gap, Diagnosis, Synthesis | cyan |
| judgement | Verdict, ComplianceVerdict, RiskAssessment, Objection, Recommendation | amber |
| analysis | AnalysisResult, ComplexityScore | violet |
| planning | ExecutionPlan, TaskAssignment, DesignSpec | blue |
| production | ArtifactProduced, VerificationResult, Document, OperationOutcome | green |
| retrospective | Proposal, Postmortem | slate |
| universal | EscalationRequest, SpawnRequest | red / pulsing |

### Reaction nodes (the event-driven primitive)
A small logic node: **when ⟨condition⟩ → ⟨action⟩**.

- **Condition** = AND-composed taps, mirroring `observe(*keys, role=)` exactly:
  signal type (rail tap) + emitter role + field predicates (`Finding.confidence
  ≥ 0.8`, `Event.q.duration > 30` — the Filter/Spec.q layer). Multiple taps
  into one reaction render the AND visually.
- **Action** = one of: **spawn** (instantiate a work-node template; the
  SpawnRequest machinery), **gate** (require approval → NodeAwaitingApproval),
  **judge** (cheap-model quality gate, Engine.judge), **escalate** (to human /
  higher tier), **notify** (Mission Control attention queue / native
  notification), **stop** (LoopDirective BREAK/CANCEL via observer control).
- **Guards** rendered on the node: budget (max spawns), depth, dedup — the
  EngineRun protections, visible as small meters instead of hidden kwargs.

### The unification
A plain dependency edge A→B is sugar for `when NodeCompleted(A) → start B`.
So one grammar subsumes all three styles — but the UI does NOT flatten them:
solid edges stay the ergonomic shorthand for sequence; rails and reaction
nodes appear only when signal logic exists. A pure playbook looks like a
clean DAG; an engine looks like what it is — a small rule machine with
budgets. Progressive disclosure, not ideology.

## 3. Run overlay — causality made visible

The designed canvas and the running canvas are the same surface (DESIGN.md
principle). During a run, fed by the existing signal SSE stream
(session_signals + ADR-0083 `lane_for` projection):

- **Node lanes**: every work node carries its lifecycle lane (queued → running
  → awaiting_approval → succeeded/failed/escalated) as border + glyph state.
- **Pulses**: each emission animates emitter-port → rail → every matching
  reaction tap. Rails carry live counters (`Finding ×12`).
- **Materialization**: a spawn reaction firing *injects an instance node* under
  its template — stacked cards, each with elapsed/cost. Watching a reactive
  flow grow itself is the product's signature image.
- **Governance visible**: judge verdicts tick pass/reject tallies on the gate;
  CapabilityViolation / EmissionRejected events flash the offending node's
  port (over-grant attempts are observable, not silent); budget meters drain;
  GateDenied marks the gate red.
- **Inspect in place**: click any node mid-run → inspector tails its stream;
  click a rail → the signal log filtered to that type; click a pulse →
  the actual payload (the typed emission, pretty-printed).

## 4. Replay — the usage-pattern discovery tool

Signals are already persisted (session_signals). History's run detail gets a
**scrubber**: replay the entire signal log over the canvas at any speed. This
is time-travel debugging for orchestration — *why did this run spawn 30 agents*
has a visual answer. Since we haven't discovered good usage patterns yet
(Ocean, 2026-06-11), replay is how we find them: dogfood runs, scrub, see
where reactions cascaded usefully vs noisily, codify what works as presets.

## 5. Authoring artifact — the spec the canvas emits

The canvas must serialize to something the engine actually runs. Today:
playbook YAML covers the static DAG; engines are configured only in Python;
reaction rules have **no declarative form**. The bridge is a spec extension —
shape sketch (final schema is implementation's job, in lionagi main src):

```yaml
# workflow spec = playbook + signal plane
steps:                      # existing playbook DAG (unchanged)
  - name: survey
    role: investigator
    emits: [finding, gap]   # capability grant, by field_name_for() key
reactions:
  - when: {type: finding, from: investigator, where: "confidence >= 0.7"}
    do:
      spawn: {template: deep_dive, operation: operate, independent: false}
    guards: {max: 10, dedup: by-description}
  - when: {type: escalation_request}
    do: {notify: attention_queue, gate: human}
budgets: {max_agents: 50, deadline_s: 1800}
judge: {model: claude_code/haiku, role: critic, at: [spawn]}
models: {survey: claude_code/sonnet, deep_dive: claude_code/sonnet}
```

A `reactions:` compiler in lionagi wires these to `session.observe(...)` —
this is new substrate work (see §6). Engine presets (research, hypothesis,
review…) become **shipped specs**: open the research engine on the canvas and
see its rule machine; fork it; tune budgets. The engines stop being opaque
Python and become inspectable, remixable designs.

## 6. Substrate work this requires (lionagi main src + CLI)

The canvas is honest only if the engine runs what the canvas shows. Gaps known
before the wiring inventory returns (to be reconciled with it):

1. **Declarative reaction compiler** — spec `reactions:`/`budgets:`/`judge:` →
   `SessionObserver.observe()` wiring + EngineRun-style guards. New module,
   likely `lionagi/orchestration/`.
2. **Grants in playbook schema** — `emits:` per step → `build_emission_operable`
   → branch capability grant. Today grants are code-path only.
3. **CLI**: `li o flow` accepts the extended spec; engine presets exposed
   (`li engine research "topic"` or equivalent — exact surface TBD after
   inventory).
4. **Studio API**: design-time CRUD for specs (likely extends existing playbook
   endpoints); run-time needs nothing new if session_signals SSE already
   carries StructuredOutput/violation/lifecycle signals (inventory confirms).
5. **Main-src adoption** (Ocean directive): operations and existing flows
   should themselves emit onto the bus uniformly (the _observe.py seam claims
   universality — verify against act/chat paths) so the canvas sees *every*
   run, not only canvas-authored ones.

## 7. What this is NOT

- Not a no-code platform. The canvas is a *visibility and composition* layer
  over typed primitives that remain code-first. YAML source toggle always
  available; the spec is hand-editable and diff-able.
- Not a new execution model. Every canvas element maps 1:1 to an existing
  substrate mechanism (observe/emit/spawn/judge/budget). If the canvas needs a
  concept the substrate lacks, the substrate gets it first — never a
  canvas-only illusion.

## 8. Phasing

1. **Render** (read-only): existing runs' signal streams over a static layout —
   lanes, pulses, counters. Validates the visual grammar against real dogfood
   runs before any authoring UI exists.
2. **Replay**: scrubber over persisted signals.
3. **Author DAG+grants**: canvas → spec with steps + emission grants.
4. **Author reactions**: rails + reaction nodes → full spec (requires the
   reaction compiler in lionagi).
5. **Engine presets as specs**: open/fork/tune the shipped engines.
