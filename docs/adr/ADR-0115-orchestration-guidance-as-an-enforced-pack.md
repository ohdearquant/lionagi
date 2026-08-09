# ADR-0115: Orchestration guidance as an enforced pack

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: cli-orchestration
- **Date**: 2026-08-08
- **Relations**: extends ADR-0043; touches the pack mechanism and the playbook spec loader

## Context

Orchestration guidance — how many workers a goal deserves, when work may be parallelized,
when a plan is too large for the window it runs in — currently lives in two places, and
neither can be tested.

The first is a long human-facing document maintained outside the framework. It encodes
real accumulated craft: a complexity score, a mapping from that score to crew size and
pattern, an independence predicate that must hold before anything is parallelized, an
economic test applied per additional worker, named anti-patterns with hard-stop semantics,
and a gate vocabulary in which an ignored rejection is an orchestration failure rather
than a judgment call. It is aimed at a human driving a chat agent, and parts of it now
describe an execution model the framework no longer has.

The second is string constants. The planner's entire guidance today is assembled in
`_run_flow_inner` as `role_roster + mode_roster + budget_note + team_guidance` and handed
to `plan()`, which prepends its own decomposition instructions. Prompt text cannot refuse
an oversized plan and cannot be unit-tested. There is no runtime signal that guidance was
*followed* — only that it was *present*.

### What the run record says the guidance has to fix

A census of recorded runs found that runs of the saved-playbook kind complete at 58% while
flat parallel fan-outs complete at 100%. The dominant named failure cluster is a timeout
cascade: one playbook did not complete in 13 of its last 15 runs, six of those dying at the
two-hour wall, with a median around 5,400 seconds. That is a sizing-versus-window problem.

### Three premises that had to be corrected before deciding

Establishing the current state changed the answer twice, so the corrections are recorded
here rather than omitted.

1. **The pack parameter is wired.** An earlier measurement found that a run could accept a
   `pack` argument with no effect while loading the same file directly resolved correctly.
   That is no longer true. The parameter now survives from the playbook spec and the CLI
   flag through orchestration setup into per-role model and mode resolution, and a default
   that fails to load raises rather than passing silently. Two independent reviews reached
   this conclusion. The argument that "the one existing pluggable mechanism of this shape
   is broken, which is evidence about how such seams fare here" is therefore stale, and it
   was the strongest argument against building on the pack.

2. **One narrower gap in that seam does remain.** Worker spec resolution attempts to load an
   agent profile named after the role *before* consulting the pack, so a profile whose name
   matches a role silently overrides the pack's model and modes, while pack-declared effort
   still applies. This precedence rule is documented only in a comment on the dry-run display
   path; nothing warns on the executing path. It is a defect to fix, not a reason to distrust
   the mechanism, but it must be fixed as part of this work or the pack's authority is
   conditional in a way no one can see.

3. **The framework already enforces guidance as code, in one place.** `plan()` states a task
   ceiling in its guidance text *and* raises when a planner overshoots it. So this is not a
   greenfield choice between text and code. A hybrid already runs; the open question is which
   layer absorbs which guidance.

## Decision

Extend the existing per-role pack into a per-run **guidance pack**: a versioned declaration
that carries the numeric thresholds and tables the planner must respect. The framework
enforces the computable subset as code at plan time, and renders the residual judgment
guidance into the planner's prompt **from that same declaration**.

Three shapes were considered and each is rejected as a complete answer.

**Guidance as prompt text alone** is the status quo this work exists to end. It cannot
refuse an oversized plan, cannot be tested, and admits no runtime evidence of consumption.

**Guidance as code alone** is right that enforcement must be code and wrong about what is
enforceable. Its two most decision-relevant inputs — how novel the work is and how risky —
are judgments a function cannot compute without a model call, at which point the judgment
has been reintroduced and hidden inside a number that looks deterministic. Encoding one
team's unvalidated coefficients into a general-purpose framework buys the false precision of
code around folklore. There is also history: two prior attempts at exactly this were built
and both died at the same step, having never been wired into the planner as constraints.

**Guidance as a pluggable declaration alone** is the text option with extra steps, because a
declaration nothing enforces changes nothing.

The composition is the ruling: the declaration provides selection and versioning, code
provides enforcement of the computable core, and prompt text becomes a rendered view rather
than a maintained source. Because the prose is generated from the same declaration the code
enforces, the text and the constraints cannot drift apart, which is the specific failure that
makes hand-maintained prompt guidance rot.

### What migrates, what stays human-facing, what is deleted

**Migrates, as declared data with enforcing code:** the independence predicate (computable
from the planned graph's declared artifact contracts and dependencies); a feasibility rule
comparing planned legs times per-leg budget against the window; a reactive spawn reserve;
crew-size bands as data keyed off feasibility; and the gate vocabulary, which the executor
already partly enforces.

**Stays human-facing forever:** the interactive elicitation flow and assumption-tagging
discipline, which are about a person and a model negotiating intent before a run exists;
and the strategic narrative material, which teaches a human and would be noise as a runtime
constraint.

**Deleted:** the complexity formula itself. This is the expensive deletion and it is chosen
deliberately. Its two highest-weighted inputs are unmeasurable without a model call, its
coefficients are unvalidated, and the production failure it would gate is better caught by
budget arithmetic on data the framework already trusts. The bands and the underlying instinct
survive as declared data and rendered prose; the false precision does not. Also deleted, as
actively wrong rather than merely stale: a blocking-foreground execution model with a batch
cap, since the framework runs detached background work; a rule assigning all version control
to the orchestrator, since worker legs commit in their own isolated worktrees; a hard-coded
model roster superseded by pack configuration; and an authoring grammar and artifact
templates the planner does not emit.

### Enforcement direction, chosen per class

The failure direction is chosen deliberately for each rule, because refusing at submit is
both the strongest option and the one that turns a bad heuristic into an outage.

- **Feasibility and sizing: refuse at plan time, fail closed, with an explicit override.**
  This is the failure mode this work exists to end. The input is arithmetic over data the
  framework already trusts, and it already enforces a task ceiling exactly this way, so a
  refusal here is a true positive rather than a heuristic misfire. An overridable hard gate
  is not an outage vector.
- **Reactive spawn reserve: refuse the marginal spawn, never the run.** When the reserve is
  exhausted, decline further spawns and record it. Fails closed on cost, open on progress.
- **Independence predicate: warn and record, fail open, never refuse.** Parallelism decisions
  are judgment-adjacent, and a false "not independent" verdict that blocks a run is precisely
  how a bad heuristic becomes an outage. Recording every verdict is what makes calibration
  possible later.
- **Gate vocabulary: enforce in the executor**, which already short-circuits dependents of a
  rejected gate. This is the one place strong enforcement is right, because the gate is the
  correctness contract.
- **Behavioral guidance: record and surface only.** A vibe cannot be refused, and attempting
  it is what turns guidance into outages.

The principle: refuse only where the check is arithmetic over trusted run data; warn and
record where it is a heuristic over judgments. The framework already splits this way — the
task ceiling refuses, an unknown assignee warns and drops. Extend that pattern rather than
inventing a second one.

## Consequences

Guidance becomes selectable per work class, so a documentation fan-out and a security audit
can carry different standards without forking code. The numbers become testable. The prose
stops being a separate artifact that can silently disagree with the code.

The cost is that the pack gains a second responsibility, and the thresholds start as
placeholders. Both are bounded below.

### Acceptance: guidance must be proven to be reached, not merely present

Each of these fails if guidance stops reaching its consumer. That is the whole point.

1. Assembling planner guidance with a known pack asserts the pack's declared thresholds
   appear verbatim in the string handed to `plan()`. Change a pack value without the prompt
   changing and this fails.
2. A plan whose leg count times budget exceeds the declared window is refused, mirroring the
   existing task-ceiling test. Proves the gate fires rather than exists.
3. With the operation ceiling set and a plan that fills it, the spawn allowance still equals
   the declared reserve rather than zero.
4. A playbook declaring a pack results in that pack's values being live at runtime. This is
   the test that would have caught the earlier wiring defect, and it must exist permanently.
5. The rendered prose is regenerated from data and compared against what the planner would
   receive, so a hand edit that bypasses the declaration fails.

Additionally, a run whose worker role shares a name with an agent profile must resolve its
model and modes from the declared precedence and say which won, closing the gap named in
context item 2.

### Bounds on this decision

This ADR does **not** authorize hard-coding a complexity formula or any novelty or risk
oracle into framework code; enforcing the independence predicate or behavioral guidance as
hard refusals; introducing a configuration system for guidance outside the pack and playbook
axes that already exist; blocking a run on anything other than arithmetic over trusted run
data; or migrating the deleted material in any form.

Extending the pack from per-role to per-run invites it to absorb everything. The per-run
additions are fenced to the items named above; anything further needs its own decision
record.

## Alternatives considered

Recorded above rather than in a separate section, because the reasoning for each rejection
is what a reviewer needs: prompt text alone fails on testability and on the absence of any
consumption signal; code alone fails on the unmeasurability of its own inputs and has a
recorded history of dying unwired; a declaration alone fails because nothing enforces it.

One further alternative was considered and rejected: deriving the sizing gate from a
complexity score computed by a model call at plan time. This preserves the formula's shape
at the cost of adding a model call to every plan, and it makes the gate's verdict
irreproducible, which disqualifies it as a refusal criterion.

## Open question for the owner

Deleting the complexity formula removes accumulated craft, not just stale text. The
recommendation is to frame it as "the bands survive, the formula does not" rather than as
"complexity scoring is abandoned," since the sizing instinct is retained and only the
five-variable arithmetic is dropped. That framing is a recommendation; the call belongs to
the owner of the original material.
