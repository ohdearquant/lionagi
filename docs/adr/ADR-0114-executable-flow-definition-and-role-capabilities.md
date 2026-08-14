# ADR-0114: An executable flow definition, and roles whose declared capabilities are real

- **Status**: Proposed (Amendment 1: 2026-08-10)
- **Kind**: Aspirational (records the target state)
- **Implementation-status**: not-started (no implementation commits identified as of
  2026-08-11; ADR-0113 D5 is blocked on the format this record designs)
- **Area**: cli-orchestration
- **Date**: 2026-08-08
- **Relations**: supersedes none; related to ADR-0113 (execution graph as the primary run
  canvas), ADR-0073 (fixed workflow-definition execution, Accepted), ADR-0023
  (dependency-aware operation graph execution kernel), #2836 (deterministic YAML pipeline
  interface), #2928 (REJECT-to-replan and the refusal budget), #2929 (role instance naming
  at the spawn boundary), #2924 (escalation children enter the DAG with no edges)

## Context

lionagi has, today, three things that each look like the beginning of a visual flow system, and
one thing that executes flows. None of the three reaches the fourth.

### What exists, measured at this commit

**A flow editor that authors a graph.** `apps/studio/frontend/src/lib/api.ts` declares:

```ts
export interface WorkflowSpec {
  version: 1;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  inputs: string[];
  outputs: string[];
}
```

`lib/workflow/serialize.ts` renders that to YAML and TOML and parses it back. It has 559 lines of
tests. Grepping the whole `apps/studio` Python tree for `WorkflowSpec` or `workflow_spec` returns
nothing: **no backend route consumes it.** The editor authors a document, serializes it faithfully,
round-trips it under test, and hands it to no executor.

**An engine that runs playbooks with no graph.** A playbook is prompt-shaped: a task description,
`max-ops`, `reactive`, declared `args`, and an artifact contract. It has no `nodes` and no `edges`.
The DAG is cast at run time by a planner model. So the engine's flows are real and its structure is
non-deterministic, which is the exact complement of the editor's problem.

**Three separate graph stacks in the frontend**, none sharing a model:

| stack | files | lines |
|---|---|---|
| designer | `lib/designer/flow.ts`, `lib/designer/topology.ts`, `components/designer/FlowCanvas.tsx` | 844 + 570 + 839 |
| workflow | `lib/workflow/flow.ts`, `serialize.ts`, `validation.ts` | 129 + 163 + 120 |
| run canvas | `lib/operationGraph.ts`, `components/canvas/` | 377 + … |
| playbook editor | `components/GraphPlaybookEditor.tsx` | 220 |

3,262 lines of implementation (plus 559 lines of tests) maintaining four opinions about what a
graph is.

### The part that changes the shape of the problem

Two further measurements, both taken by reading the executor rather than the docs, turn this from
"finish the editor" into something more interesting.

**1. Roles already declare capabilities, and the runtime ignores almost all of them.**

A role is not just a prompt. `lionagi/casts/roles/critic.py`:

```python
ROLE = Role(
    name="critic",
    emits=(Verdict, Finding),
    artifact_defaults={...},
    body="""...""",
)
```

`emits=` is a capability declaration: this role can issue a terminal `Verdict` and structured
`Finding`s. There are 40 role definitions and 23 emission types.

The two flow drivers (`lionagi/operations/flow.py`, `lionagi/cli/orchestrate/flow.py`) branch on
exactly three of those 23: `SpawnRequest`, `EscalationRequest`, and `TaskAssignment`. `Verdict`,
`Finding`, `Objection`, `Recommendation`, `ExecutionPlan`, `DesignSpec`, `Diagnosis` and the rest
appear zero times. A role declares what it can do, the model dutifully emits it, and the structure
of the run does not change. **Twenty of twenty-three declared capabilities are inert.**

**2. The gate-reject mechanism is built, correct, tested, and reached by nothing.**

The executor has a complete gate contract (`lionagi/operations/flow.py`):

```python
GATE_VERDICT_KEY = "gate_verdict"
GATE_VERDICT_REJECT = "reject"
```

A node marked `operation.metadata["is_gate"] = True` whose result carries top-level
`gate_verdict="reject"` vetoes its entire dependent subtree, transitively, and the veto is recorded
and propagated into the run's terminal reason as `COMPLETED_GATE_REJECTED`. It is careful work.

Grepping the package for a non-test writer of `gate_verdict`, or a non-test caller setting
`is_gate=True`: **there are none.** `OperationGraphBuilder.add_operation(is_gate=...)` exposes the
parameter and no production code passes it.

And the two verdict vocabularies do not meet. The `Verdict` emission model has a field named
`verdict` carrying `APPROVE | APPROVE-WITH-FIXES | REQUEST-CHANGES | REJECT`. The executor reads a
key named `gate_verdict` carrying lowercase `"reject"`. A critic that issues a textbook REJECT
produces a result with the wrong key name, on a node that was never marked as a gate, and nothing
happens. The mechanism is not off because someone disabled it. It was never wired to the roles
whose entire purpose is to drive it.

### Why this is one decision and not five

The editor cannot execute because there is no definition the engine reads. The engine's structure is
non-deterministic because nothing hands it one. Role capabilities are inert because a capability
needs somewhere to be *declared as part of a flow* before a runtime can act on it. Gates are
unreachable because nothing authors a gate node. These are four symptoms of one missing artifact: a
flow definition that is both authored and executed.

Building the editor's export without the executor's import produces a second dead format. Wiring
verdicts without a definition that can say "this node is a gate" produces a hardcoded special case.
The definition is the keystone, and #2836 is where it belongs.

## Decision

### D1 — One flow definition, authored and executed, and it is the playbook

We do not introduce a fourth format. The playbook schema grows an optional `nodes:`/`edges:`
section; a playbook that has one is executed deterministically, a playbook without one keeps
today's planner-cast behaviour unchanged. `WorkflowSpec` becomes the editor's in-memory view of
that same document, and `serialize.ts` targets it.

This makes the round trip real in both directions: a saved flow is a playbook you can run from the
CLI, and a playbook is a flow you can open in the editor.

**Rejected**: a new `.flow.yaml` alongside playbooks. Two formats means two parsers, two
validators, and a conversion nobody maintains. The playbook already carries `args`, artifact
contracts and `reactive`, all of which a flow definition needs.

### D2 — A node names a role, and the role's declaration is the node's contract

A node names a role (`critic`), not a model and not a prompt. The role supplies its model, effort,
system prompt, artifact defaults, and its `emits=` set. Config resolution stays where it is today —
by base role, which already works correctly for planned instances.

The consequence worth stating: `emits=` stops being documentation. A node's declared emissions are
the outputs the runtime will act on, which is what makes D3 and D4 possible at all.

Instance addressing (`architect-2`) is out of scope here and tracked in #2929, which must land
first or concurrently, since a definition that can name two architects makes that ambiguity
reachable from a saved document rather than only from a model's improvisation.

### D3 — Control nodes are first class, and the existing gate machinery is what they run on

A node may declare `gate: true`. The definition's parser sets `is_gate`, and the role's `Verdict`
emission is mapped onto the executor's `gate_verdict` key at the one boundary where a role's output
becomes a node result.

This is deliberately a *wiring* decision rather than a new mechanism. The veto logic, the transitive
short-circuit, the skip-reason propagation and the terminal reason code all already exist and are
tested. What they have never had is a producer.

The mapping must be explicit and one-directional: `REJECT` maps to `reject`; every other verdict
value leaves `_gate_rejections` untouched, preserving today's behaviour for non-rejecting gates.

**Acceptance**: a flow with a critic node marked `gate: true` that issues REJECT must skip its
dependents, and the same flow with the same critic issuing APPROVE-WITH-FIXES must run them. A test
that only exercises the reject arm cannot tell this wiring from a hardcoded skip.

### D4 — Reactive means the definition is a seed, not a cage

A definition describes the graph the run *starts* with. Workers may still grow it — the spawn
machinery already does this and is unchanged. Two consequences the definition must carry rather
than leave implicit:

- **Spawn budget is stated, not inferred.** `max_ops` today is one number shared between planned
  nodes and spawn capacity, so a 14-node plan under `max-ops: 16` silently gets two spawns (#2915).
  A definition that states its node count makes the remaining budget computable, and the definition
  should state the intended spawn allowance as its own number.
- **Grown nodes are structurally attached.** #2924 records that escalation children enter the DAG
  with zero edges, so nothing downstream can read their output. A definition-driven flow makes this
  worse if unfixed, because the authored graph will look complete while the grown part is
  disconnected. #2924 is a prerequisite, not a follow-up.

### D5 — Refusal is a loop with a budget, and the budget is in the definition

A gate that rejects should be able to send work back rather than only stopping it. The replan
extends the current graph — the rejected baseline, the artifacts already produced, and the gate's
own `reversible_by` are exactly the context a fresh run discards.

The definition carries the cap: how many times a gate may refuse exit before the run terminates,
with its own terminal reason so "gave up after N refusals" never presents as "completed". Design
and defaults are #2928; this ADR fixes only that the knob lives in the flow definition rather than
in a CLI flag, because it is a property of the flow, not of the invocation.

### D6 — A node reuses an agent entity across turns rather than spawning a process per turn

Today a role's branch is built once per run and reused within it (`role_base`), but a
replan or a retry starts fresh. That discards both the provider's prompt cache and the accumulated
conversation the role built up, and it is the more expensive of the two losses because the
context has to be re-derived by re-reading the same artifacts.

A definition-driven flow should default to continuing the existing branch for a node's subsequent
turns, and treat a fresh entity as the opt-in. The prompt templates should say so: a role asked to
revise its own work is being asked to continue, not to start over.

This is stated as a default with a stated exception rather than an absolute: an entity carrying a
poisoned context is exactly what a replan sometimes needs to discard, so `fresh: true` must exist
on a node and must be recorded in the run when used.

### D7 — Effectiveness is measured, or the loop is unfalsifiable

D5 introduces a loop that costs money and may change nothing. Each refusal round records what it
cost (tokens, wall clock, node count) and whether the subsequent verdict actually moved. Without
this, the refusal budget can only be set by taste, and a replan that reliably wastes three rounds
is indistinguishable from one that reliably saves a release.

The same record answers the broader question of whether a given role earns its place in a flow at
all. We have 40 role definitions and no data on which ones change outcomes.

**Scope honesty**: this ADR commits to recording the measurement, not to acting on it. An
auto-tuner that adjusts budgets from this data is a separate decision and should not be smuggled in
under a measurement requirement.

## Consequences

**The editor becomes load-bearing.** Its export is executed, which means its validation is now a
correctness surface rather than a UI nicety. `lib/workflow/validation.ts` will need to enforce what
the engine requires, and the two must be derived from one schema rather than kept in sync by hand.

**Three graph stacks become one, or the fragmentation gets worse.** Adding execution to one of four
opinions about graphs, without consolidating, produces a fifth. The consolidation is a prerequisite
for D1 rather than a tidy-up after it.

**Twenty inert capabilities become a design question each.** D2 makes `emits=` meaningful, and this
ADR only wires `Verdict` (via D3) and leaves `SpawnRequest`/`EscalationRequest` as they are. What
`Objection`, `Recommendation`, or `ExecutionPlan` should *do* to a running graph is genuinely
undecided, and pretending otherwise would be worse than leaving them declarative. They stay
declarative until each earns its own decision.

**A saved flow is a new public artifact.** Flows will be shared, versioned, and eventually
generated. `version: 1` in `WorkflowSpec` is already the right instinct; the definition needs a
compatibility statement before the first one is saved by a user rather than after.

**Prerequisites, in order.** #2929 (instance naming) and #2924 (grown nodes attach) both become
reachable-from-a-document defects once flows are authored rather than improvised. Neither is
optional and neither is large.

## Alternatives considered

**Finish the editor's export as a document format only, and defer execution.** This is where we are
now, and it is the alternative that has already been tried by default. It produced faithful
serialization with no consumer and 559 lines of tests certifying a round trip to nowhere. The cost
of continuing is another surface that looks finished.

**Make the planner emit a saved definition after the fact, rather than accepting one up front.**
Tempting because it needs no editor changes, and it is what "save this flow" most obviously means.
But it gives determinism only to reruns of flows that already happened, and it leaves authoring
where it is: impossible. Worth building as well, and it does not substitute.

**Hardcode the critic-to-gate wiring without a definition.** Cheapest path to a working reject
loop, roughly a day. Rejected because the thing that makes a gate useful is choosing *which* node
is one, and a hardcode picks for the user forever. It also leaves the other 20 capabilities exactly
as inert, which is the larger finding.

---

## Amendment 1 (2026-08-10): the compile target, values between nodes, and fan-out

D1 through D7 decide what a flow definition *is*. They do not say what it compiles to, how a value
gets from one node to the next, or what fan-out means when the degree is not known until something
runs. Those three came up as soon as anyone tried to write a definition by hand, and all three
turned out to be answerable from what the executor already does rather than from new design.

Everything below was measured at `2bff9d607` by reading `lionagi/operations/flow.py` and
`lionagi/operations/builder.py`.

## D8 — A definition compiles to builder calls, not to a graph

The compile target is a sequence of `OperationGraphBuilder.add_operation(...)` calls, one per
step, in file order. `add_operation(node_id=...)` writes `metadata["reference_id"]`, which is
already what the executor uses to name a node in progress events and what the run canvas draws, so
a step's YAML key becomes its identity end to end with nothing extra.

**The footgun this decision exists to legislate.** `depends_on` is three-valued and the two falsy
values mean opposite things. A list depends on exactly those nodes. `[]` means no dependencies, so
the node is a root of its own fan. `None`, the default, chains onto the builder's current heads
with `sequential` edges, and `sequential` edges are ordinary predecessors at execution time.

So a compiler that omits `depends_on` when a step declares no dependencies produces a **chain
where the author wrote a fan**. Every parallel step waits for the one written above it, the run is
correct, the graph draws as a line, and the only symptom is wall clock. A definition compiler must
pass `depends_on=[]` explicitly for a step with no declared dependencies, and there must be a test
that asserts the resulting graph has no edge between two steps that declare none, because a test
that only checks the run completed cannot see this.

## D9 — Fan-in needs no node kind, and authored-degree fan-out needs no new machinery

A node with N predecessors already waits for all N (`_wait_for_dependencies` awaits every
predecessor's completion event) and already receives all N results (`_prepare_operation` merges
each predecessor's result into the node's `context`). **Fan-in is therefore a property of having
several dependencies, not a node kind.** `after: [a, b, c]` is a fan-in and the definition needs no
`aggregate:` keyword.

`add_aggregation` stays available for callers that want the `aggregation_sources` metadata, and the
executor's aggregation wait is a separate mechanism that remains supported. The point is only that
a definition does not have to reach for it.

Fan-out at a degree the author knows when writing the file is N steps sharing one `after:`. That is
also nothing new. Between them, these two cover every shape a hand-written pipeline usually wants,
including the common one of several independent readers into one synthesiser.

## D10 — Data-dependent fan-out is out of scope here, with the shape it will take when it lands

Fan-out whose degree comes from an upstream result is **excluded from the first definition**. The
reason is the property that makes a static definition worth having: the whole graph is known before
anything runs, so it can be drawn, budgeted, and reviewed up front. A runtime degree breaks that by
construction, and it is a different feature wearing the same word.

Recording the intended shape, so this exclusion does not get re-litigated from scratch:

The expansion should be **deterministic and should not cost a model call**. The obvious two
implementations are both wrong for this. A planner call to decide the degree reintroduces exactly
the per-run planning cost a static definition exists to remove. A worker emitting spawn requests
does the same thing with extra steps, since something has to read the upstream result and decide.

The right mechanism already exists and is unused for this. `flow.py` exposes `on_op_complete`,
documented as the only race-free point at which a caller may inject, and `ReactiveExecutor.inject()`
schedules a pre-built operation into a running flow. A deterministic expander reads a named field
off the producing node's result and injects one node per element. No model is consulted. The
reactive executor was built for model-driven growth and turns out to serve data-driven growth
unchanged.

Two things must land before that is buildable, and neither is in this decision:

- **An injected node attaches only to its emitter.** `_accept_node` creates a single edge from
  emitter to child, so nothing downstream of the fan can depend on the fanned nodes. A definition
  that fans out and then merges is not expressible until #2924 lands. This is the blocking one.
- **Spawn capacity is shared with `max_ops`.** A fan of twelve would silently consume the reactive
  allowance, which is the same defect D4 already requires the definition to state its way out of.

Stated this way, data-dependent fan-out becomes wiring once #2924 is done rather than new executor
machinery, which is the outcome worth protecting.

## D11 — A value reaches a downstream node under the producing node's name

Measured, and this is the gap that makes hand-authoring awkward today:

`_prepare_operation` writes each predecessor's result into the consumer's context under
`f"{pred.id}_result"`, where `pred.id` is a UUID. Separately, the flow-level workspace grows only
when an operation's response carries a top-level `context` key, which the executor deep-merges.
Grepping `lionagi/operations` for a producer of `response["context"]` returns none. So **no
built-in operation puts a named value into the shared workspace**, and a prompt template has no way
to reference an upstream result by a name a human chose.

Decision: when a node has a `reference_id`, its result is also exposed to consumers under that
name. A definition's step keys are unique by construction, since they are keys in a mapping, so the
names are unambiguous without any new validation.

**This is additive, not a rename.** `lionagi/studio/services/workflow_compile.py` documents and
uses the `{pred_id}_result` key when mapping upstream context onto an engine node's positional
input. Changing the key would break that consumer silently, because it falls back to flow-level
inputs and would degrade to a plausible wrong answer rather than an error. Both keys are written.

**Rejected for now: a declared input/output assignment syntax on each step**, of the form
`"inputs -> outputs"`, where a step declares the names it reads and the names it produces, and the
runtime injects only the keys a given role has not already seen. It is the better long-run answer.
It makes the data flow checkable before the run, since a step reading a name nothing produces is a
static error, and the seen-key filter stops re-sending context a role already has. It is rejected
for the first definition on two grounds: it is a second naming concept layered on `reference_id`
rather than a use of it, and its main advantage over plain names is a token-cost saving that has
not been measured on any real pipeline. Revisit with that measurement rather than on taste.

## D12 — The safe expression evaluator moves out of the Studio layer

`StudioExprCondition` in `lionagi/studio/services/workflow_compile.py` is the only `EdgeCondition`
subclass in the package. It compiles a restricted-grammar expression with no calls, no builtins and
no globals, and it evaluates against `{"result": ..., "context": ...}`, which is exactly the shape
`_check_edge_conditions` passes to `Edge.check_condition`. It is the right evaluator and it already
works.

It is also in the Studio services layer, so the CLI and the core operations package cannot use it
without inverting the dependency direction. It moves to the graph protocol layer as the shared
condition type, with Studio keeping an alias so its own compiler is unchanged. A conditional edge
in a definition then means the same thing whichever surface authored it, which is the property that
makes an editor round trip honest.

## Scope note on structured output

A node whose work is done by an agent process passes artifacts, and its downstream consumers read
files. For those nodes structured output is not on the critical path and the definition needs
nothing for it. Structured output matters for nodes that call a model endpoint directly and parse
the reply into a schema, and for those the response format has to be nameable from the definition.
That is left open here rather than decided, because the first pipelines worth authoring are agent
pipelines and deciding a schema-reference syntax before one is needed would be guessing.

## What this amendment does not change

D1 through D7 stand as written. Nothing here introduces a node kind, a second format, or an
executor change. D8, D9, D11 and D12 are all uses or small extensions of behaviour that already
exists; D10 is an exclusion with its future shape recorded.
