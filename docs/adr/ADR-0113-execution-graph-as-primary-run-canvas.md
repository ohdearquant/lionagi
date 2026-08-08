# ADR-0113: The execution graph as the primary run canvas

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: studio
- **Date**: 2026-08-08
- **Relations**: extends ADR-0080 (six-space cockpit IA — this decides the primary surface
  *inside* a run, not the top-level taxonomy), extends ADR-0083 (operator-command protocol
  — the control verbs here ride its proposal and audit machinery)

## Context

A run detail today is a step list with a graph rendered beside it. The graph is a
read-only picture: it shows shape, and everything a user actually does — reading a
conversation, inspecting tool calls, controlling the run — happens somewhere else on the
page. That inverts what the graph is for. An orchestration *is* a graph, and the graph is
the only artifact that answers "where is this, and what is it waiting on" without the
reader already knowing the answer.

Six problems, each observed rather than assumed.

### P1 — The canvas is a picture, not a place

`RunDetail` decides whether to draw a graph (`shouldRenderAuthoredGraph`) and renders the
step list independently. Selecting a node does not make the graph the thing you work in;
detail lives in a side panel and in the list below. So the richest object on the page is
the one you can do the least with, and a user who wants to follow a run reads the list and
ignores the graph.

### P2 — Layer positions contradict what actually ran concurrently

This is the sharpest of the six because it is measurable and the measurement is
surprising. Take a graph where `a`, `b`, `c` have no dependencies; `d`, `e`, `f` depend on
`a`; `g` depends on `d`, `e`, `f`; and `h` depends on `b`, `c`, `g`.

`useLayout.ts` builds a dagre graph with `rankdir` set and no `ranker`, so dagre's default
applies. Run against dagre 0.8.5 — the version `package-lock.json` resolves for the
`^0.8.5` range in `package.json` — every available ranker produces the same assignment:

```text
ranker: (default, as configured) / network-simplex / tight-tree / longest-path
  rank 0: a
  rank 1: d, e, f
  rank 2: b, c, g
  rank 3: h
```

`b` and `c` are drawn in the third column, beside `g`, although they start at the same
moment as `a`. The layout is not misconfigured and no ranker choice changes it, because
dagre is optimizing something other than what the reader wants. Computing both objectives
on the same graph:

```text
ASAP (longest path from sources) — "what can start together"
  rank 0: a, b, c
  rank 1: d, e, f
  rank 2: g
  rank 3: h
  total edge span: 13

ALAP (what dagre produces) — "drawn next to its consumer"
  rank 0: a
  rank 1: d, e, f
  rank 2: b, c, g
  rank 3: h
  total edge span: 9
```

dagre buys four units of edge-span savings by placing `b` and `c` next to the node that
consumes them rather than next to the nodes they started with. For a *plan* that is a
reasonable trade. For a *run* it is a false statement about the run, and it is false
exactly in the case a reader most wants to see: independent work proceeding in parallel.

The horizontal axis is being asked to mean two things at once — dependency depth and
concurrency — and they disagree whenever a node's output is consumed much later than it is
produced. No layout algorithm resolves that, because it is a question about what the axis
means.

### P3 — Pause and resume exist and are invisible

The engine has had these the whole time. `DependencyAwareExecutor.pause()` installs a gate
at the next operation boundary and `resume()` releases it (`lionagi/operations/flow.py`),
the pause is
soft (operations already inside the limiter run to completion, nothing new starts), and
`lionagi/cli/orchestrate/_control.py` registers the verbs with their permitted entity
kinds:

```python
_CONSUMER_KINDS_BY_VERB: dict[str, frozenset[str]] = {
    "pause":   frozenset({"flow", "play"}),
    "resume":  frozenset({"flow", "play"}),
    "message": frozenset({"flow", "play", "agent"}),
}
```

There is a CLI subcommand for each. There is no Studio affordance for any of them. This is
a surfacing gap, not a capability gap, and it is the cheapest item in this document.

It also carries a constraint the request cannot have: **an agent run cannot be paused.**
The refusal is deliberate and the reason is in the source — an agent run drains controls at
turn end and has no pause seam inside a single `operate()` call, so a queued pause would be
read by nobody. `message` *is* supported for agent runs, landing as a warm continuation
turn. So the honest surface is pause/resume for flows and plays, and steer for agent runs.

### P4 — What a node is doing right now is not on the node

`StepNode` renders name, state, role and elapsed. It already models `running`, `paused` and
`awaiting_approval` and already respects `prefers-reduced-motion`. What it does not show is
the one thing that tells a reader whether a run is healthy: what the agent most recently
said. That lives behind a selection, in another pane, one node at a time — so watching a
live run means clicking around it rather than looking at it.

### P5 — A run's real shape cannot be saved

A run's executed DAG is not its planned DAG: reactive spawning adds nodes during the run.
And there is nothing to save it *into*. A playbook is a prompt plus planner configuration
with no field for nodes or edges, so it cannot express a graph at all; the client's own
workflow editor authors an explicit `{nodes, edges}` spec that no engine path executes.
Two representations, neither of which is an executable graph. Playbooks exist as
`*.playbook.yaml` and the client has a YAML pane, but there is no path
from "this run did something worth repeating" to a reusable definition. The interesting
artifact — the graph that actually ran, including what it grew — is discarded at the end of
every run.

### P6 — The list is load-bearing and must not be collateral

The step list is better than a graph for a long flat run, for scanning failures, and for
copying text. Making the graph primary must not remove it. This is stated as a problem
because "make X the default" requests routinely delete Y.

| Concern | Decision |
|---------|----------|
| What a run detail is | D1: The execution graph is the default canvas; node detail opens in-place on the canvas; the list is a peer view, not a fallback. |
| Layer assignment | D2: Rank assignment moves in-house and becomes ASAP; dagre keeps within-rank ordering. |
| Live node state | D3: Node cards carry the agent's latest response and activity, and every animation is driven by a real signal. |
| Run control | D4: Surface pause, resume and steer from the existing control verbs, refusing pause for agent runs explicitly rather than hiding it. |
| Reuse | D5: Export a run's executed graph as an executable flow definition (blocked on #2836). |
| View parity | D6: Graph and list are switchable peers with shared, URL-addressable selection state. |

This ADR does **not** decide:

- The top-level cockpit taxonomy — ADR-0080 D1 owns the six spaces; this changes only what
  is primary inside a run detail.
- The transport, proposal, and audit mechanics for control commands — ADR-0083 owns them
  and D4 rides them unchanged.
- Whether reactive spawning is worth its cost. That is a separate empirical question; this
  document only ensures spawned nodes are legible when they appear.
- The escalation-node attachment defect. Escalation children currently enter the graph with
  no edges, so they render as disconnected. That is a graph-semantics bug tracked on its
  own; D2 must not paper over it by inventing a position that implies an edge.

## Decision

### D1 — The execution graph is the canvas, and detail opens on it

**The contract.** A run detail renders the graph as its primary surface. Selecting a node
expands it *in place* into a detail card carrying the conversation, tool calls, artifacts
and timing for that operation. The canvas is the interaction surface: pan, zoom, select,
expand, and act.

**Exact semantics.**

- Default view for any run with a resolvable graph is the graph.
- A run with no resolvable graph (`shouldRenderAuthoredGraph` false, no operation-graph
  edges) opens on the list. A canvas with one node and no edges is not a canvas.
- Node selection is single-select and URL-addressable, so a link to "this node of this run"
  survives a reload and can be pasted to someone else.
- An expanded node grows within the layout rather than overlaying it: neighbours reflow.
  The point is to keep the surrounding structure visible while reading one node, which an
  overlay defeats.
- At most one node is expanded at a time. Multiple expanded cards on a canvas is a list
  with extra steps.
- The side panel remains for run-level content that belongs to no single node.

**Why this way.** The graph is the only view that answers a structural question without
prior knowledge. Putting the detail elsewhere forces the reader to hold the position in
their head while looking at the content, which is exactly the work the picture was supposed
to do for them. Expanding in place keeps position and content in one field of view.

### D2 — We assign ranks (ASAP); dagre orders within them

**The contract.** Rank assignment leaves dagre. A node's rank is its longest path from any
source:

```text
rank(n) = 0                                   if n has no predecessors
rank(n) = 1 + max(rank(p) for p in preds(n))  otherwise
```

dagre continues to do what it is good at — ordering nodes within a rank to minimize edge
crossings, and routing. The existing wrap and fold post-processing is unchanged; it already
operates on ranks after the fact.

**Exact semantics.**

- Nodes that can start together share a rank. In the P2 example `a`, `b`, `c` all land at
  rank 0.
- The cost is accepted explicitly: total edge span rises from 9 to 13 on that graph. `b→h`
  and `c→h` become long edges. That length is information — it says the result waited — and
  is rendered as a de-emphasized "carried forward" edge rather than styled identically to a
  short one.
- Edges always point forward: a dependent's rank strictly exceeds every predecessor's by
  construction, so no backward edges and no cycles in the drawing.
- A node with no edges at all (today, an escalation child) has rank 0 by the formula. That
  is honest — it depends on nothing — and must not be special-cased into a position that
  implies a relationship the graph does not have. It is rendered as visibly detached.
- Ranking is pure topology and needs no execution data, so the same function serves a live
  run, a finished run, and a plan being authored. One rank function, one visual grammar.

**Why this way.** The measurement in P2 is the argument. Both objectives are legitimate and
they conflict; the question is which one an execution graph exists to answer. It exists to
answer "what is happening and what is it waiting on", and that question is about
concurrency. Minimizing edge length optimizes for a tidy picture at the cost of the reading
the picture is for.

Rank-by-actual-start-timestamp was considered and rejected as the default. It is more
truthful in principle, but it requires timestamp bucketing with an arbitrary tolerance,
produces a different shape for the same plan on every run, cannot rank nodes that have not
started, and degrades to ASAP anyway whenever the scheduler is not capacity-constrained.
ASAP gets nearly all of the benefit deterministically. A time-truthful mode remains open as
a later view, not a default.

### D3 — Live node state on the node, and motion that means something

**The contract.** A node card carries, in addition to today's name/state/role/elapsed:

- the agent's most recent assistant text, truncated to a fixed line count;
- current activity (thinking, calling a named tool, streaming, waiting on a dependency);
- a token or event counter where the provider reports one.

**Exact semantics — the motion rule.** Every animation is bound to a real signal, and no
animation runs without one:

- A node pulses only while events are actually arriving, at a rate derived from the event
  rate. A "busy" animation on a stalled node is a lie, and a stalled node is precisely what
  the reader needs to spot.
- An edge animates once, on the transition, when a completed node's result becomes
  available to its dependents. It does not loop.
- Text streams into the card as it arrives. It does not typewriter-replay text that already
  landed.
- Nothing animates outside the viewport, and the number of concurrently animating nodes is
  capped, with the excess falling back to the static state. A 30-node canvas must stay
  responsive.
- Animation uses transform and opacity only, so it stays on the compositor and never
  triggers layout.
- `prefers-reduced-motion` replaces every animation with a static equivalent that carries
  the same information — a state label and a counter rather than a pulse. The hook already
  exists in `StepNode`. The reduced-motion path is not a degraded view; it is the same
  information without movement.

**Why this way.** The request for something visually alive is right, and the discipline
that keeps it from becoming decoration is that motion must be a readout. Once every
animation has a referent, "is this run healthy" becomes answerable from across the room,
which is the actual goal. It also bounds the cost: signals are finite, so the animation
budget is finite.

### D4 — Pause, resume and steer, with the refusal made visible

**The contract.** The run detail carries operation controls that map onto the existing
verbs, routed through ADR-0083's proposal and audit path:

| Control | Verb | Entity kinds | Behavior |
|---|---|---|---|
| Pause | `pause` | flow, play | Soft: in-flight operations finish, nothing new starts |
| Resume | `resume` | flow, play | Releases the gate |
| Steer | `message` | flow, play, agent | Delivered at a turn boundary as a continuation |

**Exact semantics.**

- Pause is offered for flows and plays. For an agent run the control is **shown and
  disabled**, with the reason stated: an agent run has no pause seam inside a single model
  call. Hiding it would make a deliberate engine constraint look like a missing feature and
  invite someone to "fix" it.
- Steer is offered for all three kinds, and is the answer to "I need to intervene in an
  agent run".
- The graph shows pause state on the nodes: `paused` and `awaiting_approval` are already in
  `StepNode`'s visual model. A paused run is a graph with a visible gate, not a status
  string.
- Controls are disabled, with reasons, for terminal runs.
- Because pause is soft, the UI never claims a run is stopped while operations are still
  finishing. It reports "pausing" until the last in-flight operation lands, then "paused".
  The distinction is real and a user watching token spend will notice it.
- Checkpoint resume of a *finished* run (`run_resume`) is a different operation from
  releasing a pause gate, and is labeled differently. Two things called "resume" on one
  screen is a defect.

**Why this way.** The capability is built, tested and reachable from the CLI. The only
decision left is how to present it, and the one judgment call is the agent-run refusal:
showing a disabled control with its reason teaches the model of the system, where hiding it
leaves a user to conclude the feature is missing.

### D5 — Export a run's executed graph as an executable flow definition

**The contract.** A run detail offers "save as flow", producing an **executable flow
definition** built from the graph that **actually executed** — including reactively spawned
nodes — not from the plan it started with.

**The format does not exist yet, and this decision depends on it.** A playbook today is a
prompt plus planner configuration (`name`, `model`, `agent`, `effort`, `args`, `prompt`)
with no field for nodes or edges, so the structure of every run is cast by the planner at
run time. A run's DAG therefore cannot be written into the existing playbook schema, and an
earlier draft of this decision said it could. The target is the deterministic pipeline
format tracked as #2836 — an authored spec of tasks and dependencies compiled straight to
an operation graph — and D5 is **blocked on it** rather than independently implementable.
The runtime half is already there: `DependencyAwareExecutor` executes any acyclic operation
graph deterministically. What is missing is a front end, which is exactly what #2836 scopes.

**Exact semantics.**

- The export names each node's role, model, effort and dependencies, and the artifact
  contract where one was declared.
- Spawned nodes are included as ordinary nodes. Reproducing the run means reproducing the
  shape it grew into, and a spawned node that mattered enough to keep is exactly what the
  author wants to capture.
- The export declares which nodes were spawned rather than planned, as a comment, so a
  reader can tell an authored step from a promoted one.
- Escalation children are excluded by default. They are retries, not steps, and an exported
  plan that bakes in a retry of a failure that will not recur is wrong. The exporter states
  what it dropped.
- Run-specific values (ids, timestamps, absolute paths, resolved worktrees) are not
  exported. An export that only replays on the machine that produced it is not reuse.
- Export is a client-side read of run state, so it is available for finished runs, and for
  live runs it captures the graph as of that moment and says so.

**Why this way.** The value is in the shape a run discovered, which is the part nobody can
author in advance. The alternative — export the plan — is available already by opening the
playbook, and would omit precisely the interesting part.

### D6 — Graph and list are peers

**The contract.** A view toggle switches between graph and list. The choice is persisted
per user and URL-addressable. Selection state is shared: selecting a node in one view and
switching carries the selection.

**Exact semantics.**

- Default is graph, per D1, with the no-graph exception.
- Everything reachable in one view is reachable in the other. The list is not a degraded
  mode and does not lose the controls from D4.
- The fold and wrap behavior for wide or flat graphs is retained; a flat run is exactly the
  case where a user is likely to switch to the list, and both paths must be good.

**Why this way.** P6. The list is better for scanning, for text, and for long sequential
runs, and those are real cases rather than legacy.

## Consequences

**A run becomes a place rather than a report.** The graph answers position, control and
content in one surface. This is the demo-critical property: "where is this run" reads at a
glance instead of requiring narration.

**Wide graphs get wider.** D2 trades edge length for concurrency truth, so graphs with
early-produced, late-consumed values have longer edges than before. The existing wrap and
fold post-processing becomes more load-bearing, and its thresholds should be re-checked
against the new rank distribution rather than assumed to carry over.

**One rank function serves plan and run.** Previously the same layout served both and was
tuned for neither. After D2 both use ASAP, so a plan and its run are visually comparable —
the run is the plan plus whatever it grew.

**A new failure mode: motion that outlives its signal.** If an event stream stalls, a
naive implementation leaves a node pulsing forever, which is worse than no animation
because it actively asserts liveness. Every animation needs a timeout that returns it to a
static "stalled" state, and that is a test, not a comment.

**Contributors must know** that rank assignment is ours and dagre is used only for ordering
and routing. Passing a `ranker` option to dagre after D2 changes nothing and will read as a
working knob.

**Reversal cost.** D2 is a contained change to `useLayout.ts` plus its tests; reversing it
restores the P2 behavior. D4 is additive and reversible by hiding controls. D1 and D6 are
coupled — reversing D1 without D6 leaves no default view. D5 is standalone. D3 is
standalone but its motion rules are the part most likely to erode, since each individual
decorative animation looks harmless.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Replace dagre rank assignment with ASAP in `useLayout.ts`; keep dagre for within-rank ordering. Acceptance: the P2 graph places `a`, `b`, `c` on rank 0, asserted as a unit test on the rank function, and the existing wrap/fold tests still pass. | M | (filled at issue-open time) |
| 2 | Style long-span edges as carried-forward rather than identically to short ones. Acceptance: an edge spanning 2+ ranks renders distinctly; a 1-rank edge is unchanged. | S | |
| 3 | Make the graph the default view in `RunDetail`, with the no-resolvable-graph exception. Acceptance: a run with edges opens on the graph; a single-node run opens on the list. | M | |
| 4 | In-place node expansion carrying conversation, tool calls and artifacts; single-select; URL-addressable. Acceptance: a deep link to a node reopens with it expanded. | L | |
| 5 | View toggle with shared selection and persisted preference. Acceptance: selecting in graph, switching to list, keeps the selection. | M | |
| 6 | Node cards carry last assistant text, current activity, and a counter. Acceptance: a live run shows the latest text on the node without selecting it. | M | |
| 7 | Signal-bound animation with viewport gating, a concurrency cap, a stall timeout, and a reduced-motion equivalent. Acceptance: a stalled node stops animating within the timeout and states that it stalled; reduced-motion carries the same information statically. | M | |
| 8 | Pause/resume/steer controls wired to the existing verbs through ADR-0083's proposal path, with pause shown-and-disabled for agent runs and its reason stated. Acceptance: pausing a play gates it at the next operation boundary and the graph shows the gate; an agent run offers steer and explains the pause refusal. | M | |
| 9 | Distinguish "pausing" from "paused" while in-flight operations finish. Acceptance: the state does not read paused while an operation is still running. | S | |
| 10 | Export the executed graph as an executable flow definition, spawned nodes included and marked, escalation children and run-specific values excluded, with the exporter stating what it dropped. **Blocked on #2836** — the existing playbook schema has no nodes or edges and cannot carry a DAG. Acceptance: exporting a run that spawned a node yields a definition containing it, and re-running that definition reproduces the shape. | L | #2836 |

## Alternatives considered

**Keep dependency ranking and add a concurrency cue.** Retain dagre's assignment and mark
nodes that started together with a band or shared tint. This would have bought short edges
and a much smaller change. It lost because it layers a secondary cue on a primary axis that
is still making the wrong statement — the reader's first, strongest signal remains position,
and position would still say `b` runs late. Correcting a misleading primary channel with a
weaker secondary one is how dashboards become unreadable.

**Rank by actual start timestamp.** The most truthful option for a run, and genuinely
attractive: it would show real concurrency including scheduler effects. It lost as the
*default* on four counts — it needs an arbitrary bucketing tolerance, the same plan draws
differently on every run so users cannot learn its shape, nodes that have not started have
no rank and need a fallback anyway, and with adequate capacity it collapses to ASAP. Kept
as a future optional view, where its variability is a feature rather than a surprise.

**Switch dagre to a different ranker.** The obvious first thing to try, and it is why the
measurement is in this document. All four rankers produce identical output on the P2 graph,
so this alternative does not exist in practice. Recorded because it is exactly what the next
person will attempt.

**Overlay the node detail instead of expanding in place.** Cheaper to build and it is what
the side panel already approximates. It lost because it hides the structure while you read
the content, which is the specific problem D1 exists to fix.

**Replace the list with the graph.** Simplest reading of "make the graph the default". It
lost on P6: the list is better for long flat runs, scanning failures, and copying text.

**Export the plan rather than the executed graph.** Much simpler, and already available by
opening the playbook. It lost because it omits reactively spawned nodes, which is the part
of a run that could not have been authored in advance and is therefore the only part worth
capturing from the run rather than from the plan.

**DEFERRED — a timeline or Gantt view alongside the graph.** Wall-clock duration, overlap
and idle gaps are genuinely easier to read on a time axis than on any graph, and D2's
ASAP ranking deliberately does not encode duration. The design: a horizontal track per node
ordered by rank, bars for actual start and end, shared selection with the graph and list as
a third peer under D6. Deferred rather than rejected because it is additive, answers a
question the graph is not trying to answer, and should not delay the canvas work.

## Notes

The P2 measurement was produced by running the pinned dagre 0.8.5 against the exact graph
in the problem statement, and by computing ASAP and ALAP rankings on the same graph. Both
numbers in that section are outputs, not estimates.

D4 is almost entirely presentation. The engine work was done when pause, resume and message
were built with their consumer-kind table; what was missing was any way to reach them
without a terminal.
