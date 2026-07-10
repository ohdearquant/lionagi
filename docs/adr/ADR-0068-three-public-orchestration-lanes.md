# ADR-0068: Three public orchestration lanes

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: scheduling-control-plane
- **Date**: 2026-07-09
- **Relations**: extends ADR-0069, ADR-0073

## Context

LionAGI has one dependency-aware operation-graph kernel but more than one way to define work.
The public surface must communicate whether a graph is authored or planned and whether its
topology may grow after execution starts. Hiding those distinctions behind one universal runner
would make planner calls, recovery guarantees, and control support implicit.

The current implementation exposes two reactive entry shapes. `li play` rewrites to
`li o flow -p <name>` in `lionagi/cli/main.py`, and `li o flow` plans and executes through
`lionagi/cli/orchestrate/flow.py`. Studio also executes authored WorkflowDefs without a planner
through `lionagi/studio/services/workflow_run.py`, but the equivalent headless
`li flow run <name-or-file>` command does not exist. ADR-0073 records that current fixed runner
and its migration gaps.

This ADR answers five problems:

- **P1 — Definition provenance is ambiguous.** A maintainer cannot reason about reproducibility
  unless the run says whether its graph came from a playbook, a planner response, or an authored
  fixed definition.
- **P2 — Planning authority is ambiguous.** A command that may call a planner and a command that
  must never call one cannot share an undifferentiated contract without surprising callers.
- **P3 — Topology mutability is ambiguous.** Reactive flows may admit `SpawnRequest` nodes under a
  cap; fixed workflows must reject any attempt to grow the authored topology.
- **P4 — Capability inheritance is unsafe.** Pause/checkpoint support in the reactive executor
  does not establish equivalent controls or recovery in the fixed runner.
- **P5 — Adjacent control-plane mechanisms look like runners.** Schedule triggers, durable task
  admission, workers, and dispatch delivery move or admit work; none defines a fourth way to
  author an operation graph.

| Concern | Decision |
|---|---|
| Public lane taxonomy | D1: Expose exactly reactive play, planned reactive flow, and fixed workflow lanes. |
| Shared execution boundary | D2: Give each lane an adapter and converge only at `Session.flow()`. |
| Lane capability truth | D3: Publish planning, growth, control, and recovery per lane; never infer parity. |
| Stable invocation provenance | D4: Persist a lane-tagged, source-pinned invocation envelope. |
| Scheduling and delivery boundary | D5: Treat triggers, admission, workers, and dispatch as consumers or siblings of lanes, not lanes. |

Out of scope:

- The operation-graph execution algorithm is owned by the operations ADRs; this ADR fixes public
  lane boundaries around that kernel.
- Reactive control and checkpoint mechanics are owned by ADR-0069; this ADR only constrains which
  lanes may advertise them.
- Fixed WorkflowDef compilation and current storage are owned by ADR-0073.
- Queue transition and lease semantics are owned by ADR-0071 and the target convergence in
  ADR-0072.
- Trigger evaluation and outbound dispatch delivery are owned by ADR-0070.
- Provider selection, model fallback, usage accounting, and authorization do not determine lane
  identity and are not introduced here.

## Decision

### D1 — Exactly three public lanes

LionAGI exposes exactly three public orchestration lanes. A lane is a contract for how a graph is
defined, whether planning occurs, and whether graph growth is permitted. It is not a separate
executor implementation.

| Lane | Canonical public entry | Definition source | Planner call | Runtime growth | Lane-specific control |
|---|---|---|---|---|---|
| Reactive play | `li play <name> [args...]` | Resolved playbook plus invocation arguments | Allowed and normally required through the shared reactive planner | Allowed only when reactive policy and caps permit | ADR-0069 reactive controls |
| Planned reactive flow | `li o flow [MODEL] PROMPT` or `li o flow -f <spec>` | Prompt or flow specification | Required for a new run; forbidden when replaying a checkpointed plan | Allowed only when reactive policy and caps permit | ADR-0069 reactive controls |
| Fixed workflow | `li flow run <name-or-file>` | Authored, validated WorkflowDef | Forbidden | Forbidden | Fixed-run lifecycle only |

The target CLI grammar is:

```text
li play <name> [playbook-args...] [flow-options...]
li o flow [MODEL] PROMPT [--reactive MODE] [--max-ops N] [flow-options...]
li o flow --resume <RUN_OR_SESSION_ID> [--allow-degraded-context]
li flow run <name-or-file> [--input KEY=VALUE ...] [--base-dir DIR]
```

Exact lane semantics:

- **Unknown playbook/name/file:** resolution fails before planning or execution; no empty run is
  presented as success.
- **Empty planned result:** the planned lane retries planning once and then fails loudly, matching
  the existing `FlowPlanError` behavior in `lionagi/cli/orchestrate/flow.py`.
- **Reactive disabled:** `--reactive off` selects the planned reactive lane with fixed topology for
  that invocation; it does not convert the run into the fixed-workflow lane because the graph was
  still planner-defined.
- **Checkpoint replay:** `li o flow --resume` remains in the planned reactive lane but reuses the
  recorded plan and configuration without a planner call.
- **Fixed definition with unsupported node:** compilation fails before `Session.flow()`; the fixed
  adapter does not fall back to planning.
- **Attempted fixed graph growth:** no spawn type or reactive node builder is installed, so a fixed
  run cannot accept `SpawnRequest` topology changes.

Why this way: the three questions—where the graph came from, whether a planner may change the
definition, and whether the graph may grow at runtime—produce three materially different public
contracts already visible in source. Playbooks do not form a fourth lane because
`_handle_play_shortcut()` deliberately rewrites them into the planned-flow path. A non-reactive
planned flow does not become a fixed workflow because provenance and reproducibility still depend
on a planner-produced graph.

Code anchors for current seams: `lionagi/cli/main.py` (`_handle_play_shortcut`),
`lionagi/cli/orchestrate/flow.py` (`_run_flow`, `_run_flow_inner`, `_resume_flow`),
`lionagi/studio/services/workflow_run.py` (`run_workflow_def`).

### D2 — Lane adapters converge at `Session.flow()`

Each lane owns an adapter. The adapter resolves the definition, validates lane-specific inputs,
builds an OperationGraph, stamps provenance, selects recovery/control capabilities, and presents
the result. The adapter then calls the shared graph kernel.

```text
li play ──► PlayAdapter ───────┐
                               ├─► reactive planner ─► OperationGraph ─┐
li o flow ─► PlannedFlowAdapter┘                                      │
                                                                      ├─► Session.flow(...)
li flow run ─► FixedWorkflowAdapter ─────► OperationGraph ─────────────┘
```

Target adapter interface:

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

LaneKind = Literal["reactive_play", "planned_reactive_flow", "fixed_workflow"]

@dataclass(frozen=True)
class LaneInvocation:
    lane: LaneKind
    source_ref: str
    source_kind: Literal["playbook", "prompt", "flow_spec", "checkpoint", "workflow_def"]
    source_content_hash: str | None
    inputs: dict[str, Any] = field(default_factory=dict)
    base_dir: Path | None = None
    reactive_mode: str = "off"
    max_ops: int = 0

@dataclass(frozen=True)
class PreparedLaneRun:
    graph: "Graph"
    context: dict[str, Any]
    invocation: LaneInvocation
    recovery: dict[str, Any]

class LaneAdapter(Protocol):
    lane: LaneKind

    async def prepare(self, invocation: LaneInvocation) -> PreparedLaneRun: ...
    async def execute(self, prepared: PreparedLaneRun) -> dict[str, Any]: ...
```

`execute()` must delegate graph ordering to `Session.flow()` rather than implement its own graph
scheduler. The fixed adapter passes `reactive=False` by omission and never supplies `spawn_type`,
`node_builder`, or `max_spawn`. Reactive adapters use the existing planner and may enable reactive
execution subject to D3.

Exact adapter failure semantics:

- Validation or resolution errors occur in `prepare()` and create no false completed result.
- A kernel exception propagates through the lane's existing terminalization path; adapters may
  translate it for CLI/API presentation but may not report success.
- Empty input is lane-specific: an authored definition may validly use `{}` context; a planned
  lane still requires a usable prompt/spec; a play still requires a resolvable playbook name.
- Adapter retries are not implicit. Planning retains its explicit one retry for an empty plan;
  fixed compilation and graph execution do not retry unless another ADR adds a safe policy.
- Shared execution results retain the kernel shape:

```python
{
    "completed_operations": list,
    "operation_results": dict,
    "final_context": dict,
    "skipped_operations": list,
    # reactive runs additionally expose:
    "spawned_operations": int,
    "escalated_operations": list,
    "dropped_spawns": list[dict],
}
```

Why this way: the current code already converges reactive execution through
`PlanningEngine.new_run(...).run_dag(...)` and fixed execution through `Session.flow()`. The shared
kernel owns dependency ordering, edge conditions, branch allocation, and operation results.
Keeping source resolution and presentation in adapters prevents planner concerns from entering the
kernel and prevents the fixed compiler from becoming a second executor.

### D3 — Capabilities are declared per lane

Public help, API schemas, and persisted run metadata must publish a capability matrix rather than
imply that a capability on one `Session.flow()` caller is universal.

| Capability | Reactive play | Planned reactive flow | Fixed workflow |
|---|---:|---:|---:|
| Planner may run | yes | yes for new run; no for checkpoint replay | no |
| Runtime `SpawnRequest` growth | policy-controlled | policy-controlled | no |
| `pause` / `resume` | ADR-0069 | ADR-0069 | not until separately implemented |
| Context-mode operator message | ADR-0069 | ADR-0069 | not until separately implemented |
| Reactive checkpoint replay | ADR-0069 | ADR-0069 | no |
| Fixed source/hash replay | no | no | required target provenance |
| Queue lease/restart survival | only when admitted through ADR-0072 | only when admitted through ADR-0072 | only when admitted through ADR-0072 |

Target metadata projection:

```python
lane_capabilities: dict[str, bool] = {
    "planner": ...,
    "reactive_growth": ...,
    "live_pause": ...,
    "live_message": ...,
    "checkpoint_resume": ...,
    "leased_admission": ...,
}
```

Exact semantics:

- A missing or false capability means the public binding must reject the request; it must not
  enqueue a control that no consumer will apply.
- Capability flags describe the concrete invocation, not only the lane. For example,
  `reactive_growth=False` on `li o flow --reactive off` is truthful even though the lane can support
  growth on another run.
- Queue admission is orthogonal: a direct CLI run and a queued run may use the same lane adapter
  but have different restart ownership.
- Adding fixed-run cancellation does not automatically add message injection or checkpoint replay;
  each capability needs its own consumer and terminal semantics.

Why this way: ADR-0069's poller exists only for live `flow`/`play` sessions, while the current
WorkflowDef runner installs no such poller. Advertising by lane and invocation prevents the shared
kernel from being mistaken for a shared lifecycle/control implementation.

### D4 — Every run persists lane and source provenance

Every adapter must persist enough information to answer “what exact definition produced this
graph?” without inferring from command text.

Target provenance payload:

```json
{
  "lane": "fixed_workflow",
  "source": {
    "kind": "workflow_def",
    "ref": "daily-review",
    "version": 4,
    "content_hash": "sha256:..."
  },
  "planner": {"used": false},
  "reactive": {"enabled": false, "max_ops": 0},
  "admission": {"task_id": null, "lease_attempt": null}
}
```

The fixed lane's version/hash fields are required after ADR-0073's registry migration. Until that
migration, the adapter must report the current unversioned `workflow_def_id` and omit—not invent—a
hash. Planned checkpoint replay records both the new session id and the original checkpoint
session id, matching the current `resumed_from` metadata seam.

Exact semantics:

- Name resolution pins an immutable version/hash before execution starts.
- File mode hashes the validated file bytes that were compiled.
- A source that changes after pinning does not mutate the in-flight run.
- A missing pinned version is a resolution error; the adapter must not silently use “latest.”
- The persisted lane is the selected adapter, not inferred later from `invocation_kind` alone.

Why this way: current reactive checkpoints already persist prompt, plan, configuration, session id,
and run id, while current WorkflowDef runs persist `workflow_def_id` and name in session metadata.
The target makes that provenance uniform enough for operators without pretending all definition
stores are identical.

### D5 — Control-plane mechanisms submit to or observe lanes

Schedule triggers, durable task submission, worker leases, and outbound dispatch retain separate
contracts:

```text
trigger ──► task admission ──► lane adapter ──► Session.flow
manual ───► task admission ──► lane adapter ──► Session.flow

run transition ──► dispatch outbox ──► configured external transport
```

Target routing key:

```python
action_kind: Literal["agent", "flow", "fanout", "play", "flow_yaml", "workflow"]
```

`play` routes to the reactive-play adapter, `flow`/`flow_yaml` route to the planned reactive
adapter, and `workflow` routes to the fixed adapter. Other action kinds remain non-lane task
adapters and do not increase the public orchestration-lane count.

Exact semantics:

- A trigger evaluates cadence and admission policy, then submits; it does not execute a graph.
- A worker chooses an adapter after winning a lease; producers do not import every adapter.
- Dispatch begins from a committed producer fact and never owns task status.
- A failed outbound notification cannot change the already-committed task terminal state.
- A task that is queued but has no eligible adapter/worker remains queued with a diagnosable reason;
  it is not silently reclassified as another lane.

Why this way: a lane answers how graph work is defined. Admission and delivery answer when work may
start and how an outcome leaves the process. Keeping those axes separate preserves low coupling
and allows ADR-0072 to unify lifecycle ownership without inventing more graph executors.

## Consequences

- Public command names disclose the planning boundary: `li o flow` may plan; `li flow run` may not.
- `Session.flow()` can evolve once for dependency execution while lane-specific provenance,
  control, and recovery remain explicit adapters.
- Schedulers and workers select a typed `action_kind` rather than infer behavior from arbitrary
  arguments.
- Documentation must maintain the capability matrix; adding one capability now requires changing
  the owning adapter and its advertised metadata.
- The fixed command, version/hash provenance, and adapter registry are required before the target
  surface is complete.
- Reversing D1 is expensive because command names and persisted provenance become public; reversing
  D2 is also expensive because it would duplicate the graph kernel. D3-D5 are additive and can be
  extended without changing the lane count when their boundaries remain intact.

Coupling remains bounded by the adapter registry: producers depend on one admission contract;
workers depend on one registry; each adapter depends on the graph kernel. Producers do not import
all lane implementations.

## Alternatives considered

### One universal `li run`

One command could accept a prompt, playbook, YAML, or WorkflowDef and guess the mode. It would buy a
smaller apparent CLI, but it makes planner authority and topology mutability data-dependent. The
same command could be reproducible in one invocation and planner-mutated in the next. It lost
because P1-P3 require the mode to be knowable before execution.

### Four lanes with playbooks separate from reactive flows

A playbook has its own resolution and argument schema, so treating it as a lane would make that
provenance visible. It lost because the current and intended implementation deliberately rewrites
`li play` to `li o flow -p`; planning, reactive growth, controls, and execution are the same. The
adapter distinction is sufficient without claiming a fourth execution contract.

### Two lanes: dynamic and fixed

Collapsing plays and planned flows into “dynamic” would buy an even smaller taxonomy. It lost
because playbook identity and argument validation are stable public provenance worth naming, while
the public `li play` command already exists. The three-lane names preserve that entry contract
without adding a kernel.

### Separate executor per lane

Independent executors could optimize lifecycle behavior locally. They lost because dependency
ordering, edge conditions, branch allocation, and result assembly are already shared by
`Session.flow()`. Duplicating them would create semantic drift and force every graph fix into
multiple runners.

### Treat scheduling or dispatch as additional lanes

This would make every execution transport visible as a lane and might simplify a UI menu. It lost
because schedules do not define graphs and dispatch does not admit execution. Conflating them would
make lane count grow whenever a new trigger or notification transport is added.

### Advertise the union of all capabilities

A universal capability matrix would be easy for clients to discover. It lost because the current
fixed runner has no reactive poller or checkpoint consumer. Union advertising would accept control
requests that cannot be applied, violating P4.

## Notes

The existing `li play` rewrite, planned-flow checkpoint format, reactive executor signature, and
WorkflowDef runner are descriptive source anchors only. The `LaneInvocation`, `PreparedLaneRun`,
adapter registry, `li flow run`, and uniform provenance envelope are target contracts specified by
this aspirational ADR.
