# ADR-0088: Flow-Steering Control-Plane Mechanisms

**Status**: Proposed
**Date**: 2026-07-03
**Refines / supersedes**: ADR-0085 section 3 (Queue message, in-run steering)
**Builds on**: ADR-0085 (control transport + poller) · ADR-0072 (reactive capability bus) ·
ADR-0080 (role-to-substrate routing, for op-mode `--to <role>`)

## Context

ADR-0085 shipped a control plane: `li o ctl pause|resume|msg <id>` writes a `session_controls`
row that an in-process poller applies to the live executor. Pause and resume work. The `msg`
path is built but does not verifiably work, for one reason confirmed by source and grep: nothing
reads `context.content["operator_messages"]` by name. The poller appends the message
(`cli/orchestrate/flow.py`), and `_prepare_operation` copies the whole context blob into the next
op's `parameters["context"]` (`operations/flow.py`), so the steering text reaches the model only
as one anonymous key inside a generic dict. Whether a downstream agent attends to it and changes
behavior is unmeasured and likely weak.

Two further gaps: the message affects only ops that start after it lands, so it cannot help a
single-node flow or a flow whose open ops are all past preparation (stamped
`rejected:no-pending-ops`), and it cannot redirect an op already mid-turn; and op-mode (`--as-op`)
was reserved by ADR-0085 and is unbuilt.

Field evidence from three lambda-seat interviews (2026-07-03) frames the priority. The number-one
ask is trustworthy terminal status (tracked as issue #1672), not steering. The number-two ask is
genuine pause/resume/steer for `li play`, because today the only lever for an off-track run is
kill-and-restart, discarding sunk work, and most failures are "85% right, wrong turn near the end."
This ADR is therefore explicitly subordinate to #1672 and scopes steering to the cheapest effective
intervention plus a measured gate before any machinery is built.

This ADR delivers the steering-and-injection portion of the pre-approved mechanism set
(dispatch, steering, injection, identity). Actor identity is a sibling slice and lands first;
this ADR's baseline mode does not depend on it.

## Decision

Adopt one delivery contract with staged implementation.

### 1. The steering-delivery contract (the semantics table)

A queued operator message is delivered by exactly one mode, selected at apply time:

| Condition at apply time | Mode | Stamp on success | Stamp on refusal |
|---|---|---|---|
| At least one not-yet-prepared op exists | **A: context render slot** | `applied` | — |
| No pending op, flow is reactive, `--as-op` allowed | **B: op-mode injection** | `applied` | `rejected:not-reactive` if non-reactive |
| No pending op, non-reactive, no resume-capable target | none | — | `rejected:no-pending-ops` |
| (future, capability-gated) resume-capable endpoint at op boundary | **C-sliver: resume-with-steer** | `applied` | `rejected:resume-unsupported` |

The default is A. `--as-op` opts into B. C's sliver is not in the first release and is listed so the
contract does not churn when it lands.

### 2. Mode A: the operator-message render slot (slice 1, ships first)

Render accumulated `operator_messages` as a salient labeled block in the next op's prompt, using the
same preamble pattern the flow layer already uses for budgets (`_BUDGET_PREAMBLE_TEMPLATE`,
`cli/orchestrate/flow.py`). Because the message can arrive after the DAG is built, the render must
happen at or after context-merge time (`_prepare_operation`), not at build-time like the budget
preamble. The rendered block is high-salience and instruction-shaped, for example:

```text
[OPERATOR STEER]
A human operator sent these live corrections while this flow is running.
Attend to them before continuing. Most recent last.
- <ts>: <text>
[/OPERATOR STEER]
```

The block is lifted out of the generic `context` dict so it does not also appear as raw JSON, and it
is prepended to the op's instruction. Provider-agnostic by construction, because it is prompt text.

### 3. Mode B: op-mode reactive injection (designed here, built after A is measured)

`li o ctl msg <id> "text" --as-op` builds an `operate` node from the message and calls
`executor.inject(op, independent=True)` (`operations/flow.py`). The node runs, its result lands in
`operation_results`, and synthesis sees it. Constraints (normative): reactive-only, else
`rejected:not-reactive`; the injected node draws from a separate operator budget (default 10) and
never from the model's `max_spawn`; and where an engine wraps the flow, the injected node is exempt
from the judge gate (a human already judged) but still bound by the hard budget cap and deadline.
`--to <role>` addressed injection depends on the actor-identity slice.

### 4. Provenance and discoverability (normative)

Every applied message records the op it was rendered into (a `rendered_into_op` breadcrumb in
`node_metadata`), which is both the audit trail and the join key the measurement harness needs.
`li o ctl status` shows the message text and its consuming op; `li play status` and `li monitor`
surface a steer count and last-steer timestamp; `li o flow` start output prints the steer command
hint. The measurement artifact (below) is published as the trust story.

### 5. Measurement gate (normative, blocks B)

Mode A ships with the A/B/C-arm harness in [Measurement design](#measurement-design). B is not built
until the harness shows, on at least two of the four provider families, that salient rendering (arm 2)
lifts steer-adherence over buried context (arm 1) by at least the pre-registered threshold. If A
clears the bar, B is deferred as unneeded for the common case; if A fails on a provider, the data says
exactly where B or a per-provider render tweak is required.

## Consequences

**Positive**: the built-but-dead `msg` feature becomes verifiably effective; provider-agnostic; reuses
the proven preamble pattern and the existing `inject()` seam; adds an audit and discoverability
surface that also drives adoption; the measurement gate prevents building B on faith.

**Negative / accepted**: A does not raise delivery coverage (no-carrier cases still refuse); B is
reactive-only and adds an operator-budget concept; render placement crosses a layer boundary the
budget preamble does not, so the seam choice (open question 1) carries real coupling weight; poll
latency (~2s) and soft-pause boundary semantics from ADR-0085 are unchanged.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Leave `operator_messages` in the generic context dict (status quo) | Unmeasured and likely weak; the whole point is a salient reader. |
| Mid-turn interrupt across all providers | Not feasible for codex/gemini subprocess streams or API mid-completion; boundary-only and one-endpoint-family at best. A CLI subprocess cannot inject into the middle of a running turn; killing it loses the partial turn, the exact waste steering is meant to avoid. |
| Build A and B and C together as a simultaneous build | Over-commits before measurement; the value of the tiered contract is the semantics table, not a three-front build. |
| Amend ADR-0085 section 3 in place | Buries the new render contract and measurement gate in an eight-part document; a focused 0088 is cleaner and faster to sign off. |
| Bake provider-specific transcript-resume into OSS | The live-mailbox steer (SendMessage to a running agent) is a firm-layer concern on top of OSS actor identity, not provider-specific resume logic in OSS lionagi. |

## Open questions for sign-off (Leo / Fable)

1. **Render seam.** A1 (render in `operations/flow.py` `_prepare_operation`, engine-local, mid-run
   capable, couples the engine to the `operator_messages` key) versus A2 (render at the message /
   operate construction path so any operate call, including a future single-node `li agent` steer,
   can reuse the same slot, larger blast radius). Which coupling do we accept?
2. **Accumulate or consume.** Do rendered messages persist across every subsequent op (current append
   semantics), or are they consumed once rendered so a steer does not echo into every downstream op
   for the rest of the run? Persistent is simpler; consume-once is closer to human intent.
3. **Operator budget for B.** Default size, and does it merge with or stay separate from the engine's
   budget accounting? Recommend separate; confirm the number.
4. **Threshold.** The pre-registered adherence lift and the provider count that gates B (draft: lift
   ≥ 0.4 absolute, arm 2 ≥ 0.8, on ≥ 2 of 4 providers, arm 0 ≤ 0.1). Sign off the numbers.
5. **Identity coupling.** Confirm A is shipped identity-independent and only `--to`-addressed steering
   waits on the identity slice.

## Measurement design

**The claim under test.** Rendering `operator_messages` as a salient labeled block causes a downstream
agent to verifiably change what it produces, versus the same steer left buried in the generic context
dict.

**Fixture (a redirection the base model would not pick on its own).** A two-op flow where op1 sets a
course and op2 executes it, plus a steer that should redirect op2, whose effect is machine-checkable in
the artifact, not judged by vibe. Example: op1 = "draft a short plan to implement feature X in Python";
steer (sent mid-run) = "change the target language to Rust"; op2 = "implement feature X per the plan."
Behavior change is observable as: output is Rust (`fn main`, `.rs`, no `def`). The machine check must
be unfoolable: require a Rust-only token, require the extension, and assert the absence of the
original-language token.

**Arms (the control that isolates rendering).**

- **Arm 0, no steer**: op2 follows op1's original course. Expected adherence near zero. Proves the
  steer is a genuine redirection and not what the model would do anyway.
- **Arm 1, steer buried** (current behavior): `operator_messages` present in the generic context, no
  salient render. Measures the status quo.
- **Arm 2, steer rendered** (Mode A): `operator_messages` rendered as the salient `[OPERATOR STEER]`
  block. Measures A's lift over Arm 1.
- **Arm 3, steer as op** (Mode B), optional: the steer injected as a first-class node, for comparison
  if B is prototyped.

**Metric.** Steer-adherence rate = fraction of runs where op2's machine-checkable artifact reflects the
steer, over N repetitions per arm per provider (draft N ≥ 20 per cell, large enough to see past LLM
nondeterminism). Run per provider (claude_code, codex, gemini, one API model): provider-agnostic is a
hypothesis about the transport, not a guarantee about how each provider's prompt formatter surfaces the
block. The transport is provably provider-agnostic; the rendered effectiveness is what is measured.

**Success gate (pre-registered, the number that decides B).** Arm 2 adherence minus Arm 1 adherence
≥ 0.4 absolute, and Arm 2 adherence ≥ 0.8, on at least two of the four provider families, with Arm 0
adherence ≤ 0.1 to validate the fixture. If Arm 2 clears the bar, next-node rendering is sufficient for
the common case and B is deferred. If Arm 2 fails on a provider, that is the empirical trigger to build
B or a per-provider render adjustment, and the data names the provider.

**Confounds to control.** The steer must be a true redirection (Arm 0 guards it). Fix temperature and
seed where the provider allows, else raise N. Make the machine check unfoolable (token plus extension
plus negative token). Run per provider. Sweep block position (before versus after the task text) as a
small secondary variable, since salience is partly positional.

**Harness home.** Ride the existing orchestration benchmark harness (`benchmarks/orchestration/`) with a
new steering fixture and an arm switch; emit a provider-by-arm adherence table as the committed evidence
artifact. The only new instrumentation is the `rendered_into_op` breadcrumb from the provenance section,
which joins "steer delivered" to "op that saw it" to "artifact produced." One breadcrumb, three payoffs:
audit, discoverability, and measurement.

## Implementation fences (slice 1)

- **MAY**: add a reader that lifts `operator_messages` from the merged context and renders a salient
  labeled block into the next op's instruction, mirroring `_BUDGET_PREAMBLE_TEMPLATE`. MAY add the
  `rendered_into_op` breadcrumb. MAY add the `benchmarks/orchestration/` steering fixture and arms.
- **MAY NOT**: touch the pause/resume gate (`operations/flow.py`) or its tests. MAY NOT change the
  `session_controls` transport, the poller stamp semantics, or the `rejected:*` codes. MAY NOT build B
  (op-mode `inject`) in slice 1. MAY NOT add any provider-specific mid-turn interrupt. MAY NOT claim A
  works without the harness table.
- **Verify by**: (1) a regression test asserting pause/resume and `rejected:no-pending-ops` are
  unchanged; (2) a unit test asserting a queued message renders as the labeled block in the next op's
  instruction and is absent from the raw context JSON; (3) the provider-by-arm adherence table meeting
  the pre-registered gate before B is scheduled.
