# ADR-0116: Editor client capability expansion

- **Status**: Proposed
- **Kind**: Aspirational (records the target state)
- **Area**: studio
- **Date**: 2026-08-07
- **Relations**: extends ADR-0082; revises ADR-0082 D2; depends on ADR-0076, ADR-0078

## Context

ADR-0082 documents the VS Code extension as a **read-only** native client of the Studio
daemon. Its D2 is not an implementation detail but a deliberate negative capability:

> The client exposes no POST, PUT, PATCH, or DELETE application method. [...] Adding launch,
> schedule, definition, approval, or maintenance methods requires a new decision, not an
> opportunistic helper.

This ADR is that decision. It exists because a capability survey of the daemon's public
surface found that almost everything worth adding to the editor crosses D2, and because
D2's own alternatives section names precisely what was missing at the time: "no
editor-specific confirmation/authorization design."

**P1 — The valuable capabilities are mutations.** A survey of the daemon's registered routes
classified each capability by whether it belongs in an editor at all. The capabilities that
scored highest for editor fit — resume a stopped run, cancel a runaway invocation, work an
attention queue, launch a playbook, edit a versioned agent or playbook definition — are all
POST/PUT/DELETE. The read-only capabilities that remain unported are mostly the ones with no
editor-specific advantage.

**P2 — The daemon is not yet a contract for a second mutating client.** A readiness review of
the same surface found that its route handlers return generic containers rather than declared
response models, so the payload shape a second client codes against is established by
inspection rather than by a schema. Separately, authentication is optional and, when enabled,
global and unscoped: a token that permits reading a run also permits every mutation on the
surface. ADR-0082 D6 makes OpenAPI and recorded fixtures the compatibility seam; that seam is
thinner than D6 assumes.

**P3 — A read client fails safe and a mutating client does not.** Under ADR-0082 the worst
outcome of a contract drift is a blank or stale view. Once the extension can cancel, launch,
or delete, the same drift can act on the wrong subject. The blast radius of the compatibility
gap in P2 is a function of D2, which is the thing this ADR changes.

**P4 — Some Studio capabilities should never reach the editor, for reasons other than cost.**
VS Code now ships native MCP server management through its own settings, so a second
management surface inside the extension would duplicate a capability the host already owns and
does better. The visual workflow canvas is not a shipped Studio feature and is the textbook
case for staying in the browser. Application theme and locale preferences belong to the host.
Some routes have no frontend consumer at all today.

**P5 — Streams still do not resume.** ADR-0082 delta 3 records that session and signal streams
have no retry, backoff, or resume contract, and that a transport EOF surfaces as an error
rather than a recoverable state. Every capability added on top of those streams inherits that
gap, and a mutation whose confirmation arrives over a stream that can silently end is worse
than a read that does.

**P6 — One entry point, or none.** The strongest editor-native capability in the survey is a
conversational operator inside the editor, which is also the largest single build. The risk it
carries is not cost but duplication: an editor already hosting a chat-driven agent surface
gains little from a second one that does not share its affordances.

| Concern | Decision |
|---|---|
| Capability boundary | D1: Replace D2's blanket read-only rule with an enumerated, tiered mutation allowlist. |
| Sequencing | D2: Ship editor-native reads first; gate broad mutation on the daemon contract work. |
| Confirmation | D3: Every mutation is user-initiated on a visible subject and confirmed against a named subject. |
| Scope exclusions | D4: Record the capabilities that are deliberately never ported, with reasons. |
| Stream dependency | D5: A capability may not depend on a stream for its completion signal until resume exists. |

Out of scope:

- Changing the daemon's HTTP/SSE contract. ADR-0076 and ADR-0078 own it; this ADR states what
  it needs from that contract and sequences behind it.
- Converging the editor's visual design with the browser cockpit. ADR-0082's position stands.
- Marketplace packaging, signing, and release workflow. That is separate work.
- Embedding the Studio SPA. ADR-0082's alternatives section settled it and nothing here
  reopens it.

## Decision

### D1 — An enumerated mutation allowlist replaces the blanket prohibition

ADR-0082 D2 is revised, not deleted. The negative capability it establishes was correct for a
client with no confirmation design, and the property worth keeping is that the mutation surface
is **closed and enumerated** rather than open. What changes is that the enumeration becomes
non-empty.

The client gains mutation methods only for capabilities named in a tier below. A capability not
named in a tier is not addable by an opportunistic helper, and the negative-capability test in
ADR-0082 D6 is amended rather than removed: it asserts the client's mutating methods are
exactly the allowlisted set, in both directions. A method present in the client and absent from
the allowlist fails the test, and so does the reverse.

This keeps the property that made D2 valuable. The test that used to prove "no mutations"
proves "these mutations and no others," which is the same kind of claim.

### D2 — Three tiers, sequenced, with the gate stated

**Tier 1 — editor-native reads. No change to the mutation surface; ships first.**

These are additive to ADR-0082 and do not touch D1's allowlist:

- File decorations and CodeLens driven by the file paths already present in tool-call message
  arguments, so a run's touched files are visible in the editor's own explorer and gutter.
  This is the strongest editor-native affordance found in the survey and it is read-only.
- Scoping the run tree to the open workspace's detected project, so the editor shows the runs
  belonging to the code in front of the user rather than the whole machine's history.
- Ambient counts in the status bar from the aggregate stats route. Counts, not charts: a
  sidebar has no room for a chart that earns its pixels, and the text form carries the same
  information.

**Tier 2 — bounded mutations. Revises D1's allowlist; requires D3's confirmation design.**

Three capabilities, chosen because each acts on a subject the user is already looking at, each
has a small blast radius, and none creates new work:

- Cancel a running invocation.
- Resume a stopped or failed run with a follow-up.
- Record, revise, and undo a disposition on an item needing attention.

Cancel and attention dispositions are recoverable or explicitly reversible. Resume creates work
but only as a continuation of a run the user selected, and its failure mode is a duplicate
continuation rather than a destructive act.

**Tier 3 — gated on the daemon contract. Not implementable until the gate opens.**

- Launching a playbook or a single-agent run.
- Creating, editing, versioning, and rolling back agent, skill, and playbook definitions.
- Creating and triggering scheduled automations.

The gate is P2, stated as two conditions on the daemon rather than on the extension:

1. The routes a tier-3 capability calls declare response models, so the extension codes against
   a schema rather than against an inspected payload.
2. Authentication distinguishes read from mutation, so an editor token can be issued that
   observes without being able to launch or delete.

Until both hold, a tier-3 capability in the extension would be a mutating client coded against
an undeclared shape and authorized by a token that cannot express its own limits. Tier 3 is
therefore blocked on ADR-0076/ADR-0078 work, and this ADR does not authorize shipping it early
on the grounds that the individual feature is small.

Definition editing is the capability most likely to be argued into tier 2 on the grounds that
it is "just text." It stays in tier 3: a definition write is the mutation whose blast radius
outlives the run that triggered it.

### D3 — Confirmation binds to a named subject, not to a click

Every tier-2 mutation follows the same shape:

- It is initiated by the user from a view where the subject is displayed. There is no
  mutation reachable from a command palette entry that takes an identifier the user has not
  seen rendered.
- The confirmation names the subject in the same terms the view named it. A cancel prompt
  states which run and which invocation, not "cancel this?".
- The request pins the subject identifier read from the displayed record. It is not re-derived
  from a selection that may have changed while the prompt was open.
- The result is a state read, not a status code. A mutation reports success only after reading
  back the subject's state. A 2xx with an unchanged state is a failure and says so.

The last point is the one that matters most and it is the one ADR-0082's read-only client never
needed. A mutating client that reports the outcome of its own request rather than the state of
the subject will report success for a request that was accepted and dropped.

### D4 — Deliberate exclusions, recorded so they are not rediscovered

These are not backlog items. They are decisions:

- **MCP server management.** VS Code ships native MCP configuration. A second management
  surface inside the extension duplicates a host capability and will diverge from it.
- **The visual workflow canvas.** It is not a shipped Studio capability, and if it ships it is
  pointer-and-canvas work, which is the clearest browser-only case in the survey.
- **Application theme and locale preferences.** The host owns these. The extension should
  inherit them rather than carry a parallel settings surface.
- **Charts and sparklines.** The information survives the translation to text counts; the
  pixels do not survive the translation to a sidebar.
- **Routes with no current consumer.** A capability with no frontend consumer in Studio today
  is not a capability the editor is missing.

### D5 — No completion signal over a stream that cannot resume

Until ADR-0082 delta 3 is closed, no capability may take its completion or confirmation signal
from a session or signal stream. A tier-2 mutation confirms by reading the subject's state
(D3), which is a request that either succeeds or fails visibly.

This constraint is what keeps P5 from turning a known read-side gap into a mutation-side
correctness bug: a stream that silently ends leaves a read view stale, but it would leave a
mutation view lying.

## Consequences

- The editor gains the capabilities that make it worth opening, in an order where the cheap
  read wins do not wait on the contract work.
- The negative capability that made ADR-0082 D2 valuable survives as a closed allowlist with a
  two-directional test, rather than being traded away for the first useful mutation.
- Tier 3 is explicitly blocked on daemon work that this ADR does not own, which means this ADR
  can be accepted without implying that launch and definition editing are near.
- The daemon acquires a second client with mutation expectations, which raises the cost of an
  unversioned response change from "one client renders blank" to "one client acts wrongly."
  D1's allowlist test and the tier-3 gate are the compensating controls.
- Confirmation-by-state-read is more work per mutation than reporting a status code, and it is
  the difference between a client that reports what it asked and one that reports what happened.
- A conversational operator in the editor is deliberately not in any tier. It is the largest
  build in the survey and the one most likely to duplicate an affordance the host already
  provides. It should be decided on its own evidence, not carried in on the momentum of this
  expansion.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Amend the ADR-0082 D6 negative-capability test to assert the client's mutating methods equal the tier-2 allowlist exactly, failing in both directions. Tier 1 contributes no mutating methods, so it is not part of that population. | S | (filled at issue-open time) |
| 2 | Declare response models on the routes any tier-3 capability calls, so a second client codes against a schema. | L | (filled at issue-open time) |
| 3 | Separate read authorization from mutation authorization so an observer token can be issued to the editor. | M | (filled at issue-open time) |
| 4 | Close ADR-0082 delta 3 (stream retry/backoff/resume with a visible recoverable state) so D5's constraint can be lifted. | M | (filled at issue-open time) |
| 5 | Add confirmation-by-state-read as a shared helper so each tier-2 mutation does not re-implement it. | S | (filled at issue-open time) |

## Alternatives considered

### Keep ADR-0082 D2 unchanged and ship only reads

This preserves the fail-safe property completely and needs no confirmation design. It lost
because the survey found the read capabilities that remain unported are mostly the ones with no
editor-specific advantage, so the extension would accumulate work without becoming more useful.
The negative capability was protecting against an absent confirmation design, and the remedy for
an absent design is to write it.

### Open the mutation surface generally and rely on the daemon's authorization

This is the smallest amount of extension work: expose the routes and let the daemon refuse what
it should refuse. It lost on P2. The daemon's authentication is currently global and unscoped, so
"rely on the daemon" resolves to "rely on a token that permits everything," and the extension
would be the surface through which that token becomes easy to use by accident.

### Ship tier 3 first because launching is the most requested capability

Launch is the capability that most changes what the extension is for. It lost on sequencing
rather than on merit: it is a mutation that creates unbounded new work, coded against undeclared
payload shapes, authorized by an unscoped token. Every one of those three is fixable, and this
ADR prefers fixing them to shipping ahead of them.

### Make the editor a thin launcher into the browser cockpit

Rather than porting capabilities, deep-link each one into the running Studio SPA. It lost
because it inherits the cockpit's navigation for tasks the editor could do in place, and because
the deep-link target is a browser context that does not know which file the user is looking at,
which is the only thing the editor knows that the browser does not.

### Build the conversational operator first as the single strategic bet

Chat-driven control is the most editor-native shape in the survey and would subsume several
smaller capabilities. It lost for this ADR because it is the largest build, because it is the
capability most likely to duplicate what the host already offers, and because accepting it here
would bundle a large product decision into a capability-boundary decision. It is deferred, not
refused.
