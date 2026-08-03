# ADR-0066: `li mcp` v2 verb surface — one tool, generated per-verb schemas

- **Status**: Accepted
- **Kind**: Aspirational
- **Area**: cli-surface
- **Date**: 2026-07-24 (amended 2026-07-25 twice — D1, D2, D3 and D7; amended 2026-07-27 —
  D6; amended 2026-08-03 — D6, see Amendment history)
- **Relations**: builds on ADR-0095 (run-terminal callbacks — the `notify.on_terminal`
  layer the MCP submits ride) and ADR-0104 (`li kill` transitive play reaping and
  terminal-notify on kill, whose semantics the kill verb must inherit rather than
  re-implement); none superseded

## Amendment history

**2026-08-03 — Amendment 4: D6's wait contract catches up with the observed surface.**
Three changes, all already true of the implementation, recorded under this document's own
rule that a non-terminal classification is added to a list a caller can read, never left
to the classifier alone.

1. **The partition is four-way.** An aged `preparing` record has no process to prove
   absent, so it is neither `pending` (waiting has stopped changing its answer) nor
   `stopped_without_end` (nothing observable stopped); it is returned in its own list,
   `unresolved_spawn`, with the age threshold echoed in the result. `stopped_without_end`
   is also now stated by evidence rather than phase: it holds records that stopped or
   cannot be shown to be progressing, including a conclusive finding whose transition
   could not be published and a record from before the spawn phase was recorded.
2. **The one-poll floor keys on either special list**, since neither resolves by waiting.
3. **Observation purity gains its one fenced exception.** The first reader of a
   conclusively-gone `started` run may durably reap it where the fenced write publishes;
   a refused write leaves the record untouched. ADR-0106 D10 and ADR-0107 carry the full
   statement; it is recorded here so a verb-surface reader is not told purity is
   unconditional.

**2026-07-27 — Amendment 3: D6 states four things it previously left to the reader.** All
four were already true of the implementation and none was findable in this document, which
is the whole problem: a decision that lives only in code is not a decision anyone can build
against, and each of these had a consumer reading the silence a different way.

1. **Every entry carries `outcome` beside `terminal`.** The original enumeration named kind,
   status, terminality and reason code. A caller given only those has to map an open status
   vocabulary onto success and failure itself, which is the copy of our status names that
   this surface exists to make unnecessary.
2. **Observing does not touch the run.** D6 said nothing about signals or disconnects, so
   "cancel the wait" and "cancel the work" were indistinguishable from the document. They
   are different operations and always were.
3. **The result partitions the observed ids.** D6 named the pending list without saying
   which ids qualify for it. That silence was read three different ways in one review round,
   which is how a reader-derived rule announces that it needs writing down. The partition is
   now stated outright.
4. **A call carries a minimum duration when an id resolves nothing by waiting.** This is
   caller-visible timing, so leaving it unstated recreates the same silence one level down:
   a client measuring a call that returned in one poll interval rather than immediately had
   no document that explained it. Stated as a floor, with the two cases that do not pay it.

The third is the one worth flagging for anyone tracing history: it is a *definition* of
something this document left open, not a reversal. An implementation that put every
non-terminal observed id in `pending` was conforming before this amendment. It is not
conforming after it, and that is the point of amending rather than assuming.

The same three rules are stated for the machine result contract in ADR-0106, which is
Proposed at the time of writing. They are recorded here on their own merit and do not
derive authority from it; where the two documents overlap they are meant to agree, and if
they diverge this one governs the verb surface.

**2026-07-25 — Accepted.** The status field is the authority on a document's stage and nothing
infers acceptance from an edit, including an edit by the author of the decision. Recorded here
rather than derived: the first amendment below was authored in place at `e082e02f0`, and the
second was gated separately before landing.

**2026-07-25 — Amendment 2: `extra_args` removed from D3 and D7; D2 requires a schema
fingerprint on spawn verbs.** The escape hatch is deleted rather than retained, because
retention is a standing obligation to re-prove that no opaque token sequence introduces a new
command boundary, and D2/D3 removed the premise that made the hatch useful. The fingerprint
makes schema agreement a property of the protocol: a spawn op carries the fingerprint targeted
help returned, so the schema a caller validated against is the schema that runs. Its guarantee
is stated at the strength it actually has — agreement always, exposure only for the caller that
fetched it, since a fingerprint can be inherited.

**2026-07-25 — D1 reversed from a discrete core to a single tool; D2 extended.** The
original D1 kept nine high-frequency operations as individually advertised tools alongside
one dispatch verb. That is now one tool with every operation behind it as a namespaced verb.
D2 gains two requirements that make the single surface usable: catalog entries carry one-line
signatures rather than bare names, and a rejected op returns the schema it was judged
against.

Three drivers, in the order they carry weight:

1. **Internal inconsistency.** D2 requires on-demand schema resolution because advertised
   schemas are expensive. The original D1 then exempted the nine most expensive schemas from
   that rule. The amendment applies D2 to the surface it was written for.
2. **Measured cost.** The v1 server's seven tools advertise 8,683 bytes, roughly 2,170
   tokens, into every session of every caller; the three submit tools are 81% of it. A
   discrete core lands near 13KB, and one tool per CLI subcommand near 72KB.
3. **Precedent.** Single-tool MCP surfaces fronting several times this verb count are in
   daily production use without ergonomic complaint.

Also corrected: the original D1 headed its list "Discrete core (10 tools)" while enumerating
nine. The superseded shape was nine discrete tools plus `request`.

## Depth contract

This ADR decides the shape of the MCP verb surface, not its implementation schedule. The
surface measurements below were taken from the built parsers at commit `44ec77901`, and the
kill semantics in D5 were verified by source read of `cli/kill.py` at the same commit, not
from the prose of the ADR that introduced them. Where a claim depends on an existing
contract, the contract is named with its file.

The amendment's byte figures were taken by an MCP `tools/list` handshake against the running
v1 server, not estimated from source. The gate 1 parser inventory recorded below was taken by
driving `cli/main.py`'s own `_build_parser` seam rather than a reconstruction of it.

## Context

`li mcp` (ADR-less, shipped in the `li mcp` change) serves an MCP server over stdio that
submits `li` runs as detached background jobs. v1 exposes seven tools: three submits
(`submit_agent`, `submit_flow`, `submit_fanout`) and four job operations (`job_status`,
`job_output`, `job_kill`, `jobs_list`).

The CLI it fronts is much larger. Measured at `44ec77901`:

| | count |
|---|---|
| top-level command groups | 17 (`orchestrate`/`o`, `agent`, `casts`, `engine`, `team`, `studio`, `schedule`, `state`, `invoke`, `kill`, `mirror`, `monitor`/`mon`, `dispatch`, `doctor`, `stats`, `plugin`, `hooks`) |
| special-cased commands | 3 (`play`, `wait`, `mcp`) |
| subcommands | ~48 (schedule 14, state 8, team 5, dispatch 5, plugin 5, orchestrate 3, invoke 3, hooks 2, engine 1, studio 1, stats 1) |
| flag depth on the spawn path | `li o flow` ~36 distinct flags, `li agent` 25, `li monitor` 11, `li kill` 7 |

So an MCP client can reach three spawn operations out of roughly sixty addressable ones.
Everything else — scheduling unattended runs, waiting on completion, inspecting run state,
team messaging, monitoring — is reachable only by a human at a terminal.

Two failure modes shape the design. First, a tool list that grows one discrete verb per CLI
subcommand would reach ~60 tools, which is a worse client experience than the CLI and
guarantees drift as the CLI moves. Second, a single free-form dispatch string would make
every call a quoting problem, because the primary payload on this surface is free-text
prompts carrying arbitrary quotes and newlines.

## Decision

### D1 — One tool, every operation a namespaced verb behind it

The server advertises exactly one tool. Every operation is a verb reached through it.

```text
request(ops=[{op, args}, ...], help?)
```

Verbs are namespaced strings that mirror the CLI's own structure:

- spawn: `agent.submit`, `flow.submit`, `fanout.submit`, `play.submit`
- observe: `job.status`, `job.output`, `job.list`, `job.wait`
- control: `job.kill`
- long tail: `schedule.*`, `team.*`, `state.*` (read-only subset), `dispatch.*`, `monitor`,
  `stats`, and future additions

`play.submit` and `job.wait` are new; the rest are v1's seven operations renamed into the
namespace. Nothing that was reachable becomes unreachable.

**The tool-level change is a clean break; the verb-level change is not.** v1's seven tools
cease to exist when this ships. They are not kept as thin shims for a deprecation window,
because those shims *are* the advertised schema this decision exists to delete — a window
would pay the full cost for its whole duration and deliver the benefit only at its end.
Clients pick up the new surface by reloading, which is one action rather than a migration.

Verb-level continuity is cheap and is kept: v1's operation names (`submit_agent`,
`job_status`, and the rest) are accepted as synonyms inside `ops`, resolved to their
namespaced form before dispatch, and **absent from the catalog** so they are never
advertised or taught. They exist for callers already scripted against the old names. They
are accepted for exactly one release and removed in the first minor release after the one
that introduces this surface, and no later than 2026-09-30.

**Ops are JSON, never a DSL string.** `ops` is a list of objects:

```json
{"ops": [{"op": "schedule.list", "args": {"limit": 20}}]}
```

A string-DSL escape hatch is explicitly rejected. The payloads this surface carries are
prompts — arbitrary text with quotes, newlines, and braces — and any DSL would require
hand-rolled escaping at every call site for no gain over JSON the client already speaks.
A string-DSL dispatch form reads cleanly on surfaces whose argument values are short
structured scalars. It is the wrong borrowing here, because on this surface the argument
value *is* the prompt.

**Why one and not a discrete core.** An advertised tool schema is not free. It is sent to
the model on every request, in every session, by every caller, forever. Measured against the
v1 server: seven tools advertise 8,683 bytes, roughly 2,170 tokens, and 81% of that is the
three submit tools alone at 19-20 parameters each. A discrete core of nine or ten lands near
13KB. Giving each of the CLI's ~60 addressable operations its own tool would land near 72KB,
past the point at which a flattened parameter surface has already been observed to exceed
what a client will accept.

The deciding argument is internal consistency rather than the byte count. D2 requires
schemas to be resolved on demand *because* schemas are expensive. A discrete core exempts
the ten most expensive schemas from the rule that exists to control exactly that cost. One
uniform surface applies D2 to everything it was written for.

Keeping a single "hot" verb as a discrete tool is rejected for the same reason: it reopens
the exemption argument for every verb that later becomes frequent, and the uniformity is the
property worth having.

The ergonomics are not speculative. Single-tool MCP surfaces fronting dozens of namespaced
verbs are already in daily production use, and remain fluent at a verb count several times
larger than the one proposed here. The prior is that a single dispatch tool over a verb
space this size works, not that it is a risk being taken.

### D2 — `help` returns a live, parser-derived schema, resolved on demand

`request(help=...)` returns parameter schemas **generated from the CLI parsers at runtime**,
never a hand-maintained table.

Documentation lags the code it documents; a generated schema cannot. This is the single
convention that keeps a dispatch surface honest as the CLI moves underneath it, and it is
the reason a dispatch verb is acceptable at all: without a live schema, `request(...)`
would be an undiscoverable string interface.

**Help is selective, and the schema is never published eagerly.** With no target, `help`
returns the allowed-verb catalog. With a verb named, it returns that verb's full schema. The
MCP tool's own advertised schema stays small: it describes `ops` and `help`, not the union of
every reachable verb's parameters.

This is a hard constraint, not a preference. Flattening a large parameter surface into a
single advertised schema is a known failure mode — a schema that grows into the tens of
kilobytes is sent to the model on every request and can exceed what a client will accept,
which takes down the whole tool rather than degrading one verb. Discovery must therefore be
a call, not a payload.

**The catalog carries one-line signatures, not bare verb names.** A list of names tells a
caller what exists and not how to invoke it, which forces a second call before any first
call. A signature — the verb, its required parameters, and a one-line summary — is enough to
write the common invocation directly, and still costs a fraction of a full schema. Full
parameter detail stays per-verb and on demand.

**A rejected op returns the schema it was judged against.** When an op fails argument
validation, the error carries that verb's expected schema inline. The first mistake then
costs one round-trip and teaches the shape, instead of costing a rejection followed by a
separate `help` call. This is what makes a single dispatch tool fluent in practice rather
than merely compact: the surface repairs the caller as it refuses them. Closed validation
(D7) supplies the rejection; this decides what the rejection must contain.

**Spawn verbs require the fingerprint that targeted help returns.** `help="<verb>"` returns,
alongside the verb's full projected schema, a `schema_fingerprint` derived from that schema's
content. An op naming a spawn verb (`agent.submit`, `flow.submit`, `fanout.submit`,
`play.submit`) must carry the current `schema_fingerprint` for that verb; an op without one,
or with one that no longer matches, is rejected.

Collapsing the surface to one tool makes discovery a call. It does not make discovery happen.
A caller can read a one-line catalog signature and invoke a thirty-parameter verb having never
seen the rest of it, which is the condition this surface exists to end.

State the guarantee at its real strength. The fingerprint proves that the schema the caller
validated against is the schema that will run: agreement at spawn time, staleness impossible.
For a caller that fetched it, it also proves exposure — the parameters were in that caller's
context before it was allowed to spawn. It does **not** prove exposure in general, because a
fingerprint is a transferable string: a parent that pastes the current value into every
spawn-prompt template passes children through the gate who never saw the schema, which is
shallow usage reproduced one layer up. The mitigation for that is convention, not protocol —
spawn prompts do not carry fingerprints — and staleness-rejection alone justifies the
round-trip. Before proposing a gate, ask who controls its input: here the caller does, so this
gate proves a fetch happened, not that anything was read.

**The rejection carries its own remedy, in band.** A rejected op returns the exact `help` call
to make and the current fingerprint in the same response. Recovery is one step. A rejection
that reports only "stale fingerprint" strands precisely the naive caller the requirement exists
to protect. This is the same distinction that makes per-call rejection acceptable where
server-level refusal is not: refusing to serve because the server is behind fails toward
darkness, while an op-level rejection whose recovery is one in-band call fails toward a
round-trip.

**Scope is spawn only, and the exclusions are deliberate.** `job.kill` is exempt: it is an
emergency operation with a trivial argument surface, and gating the stop of a runaway run
behind a discovery round-trip is the one place this friction could cost something real. The
read verbs (`job.status`, `job.output`, `job.list`, `job.wait`) are exempt: their argument
surfaces are small and a wrong read is self-correcting. Widening the requirement to control or
read verbs needs its own decision, because the argument that justifies it on spawn — a wide
parameter surface where a silently missed parameter does real damage — does not hold there.

Generation mechanics are decided in D3.

### D3 — Schema generation: runtime projection of the built parsers

Schemas are produced by **runtime introspection of the argparse parser that the CLI itself
builds**, behind one narrow, isolated projection module. Neither of the alternatives is
taken: the CLI is not refactored onto a declarative registry for v2, and no schema artifact
is generated at build time.

`cli/main.py` already centralizes command discovery in `_COMMAND_REGISTRY` and constructs
only the selected command's real parser. The projector reuses that seam, so it reads the
same parser a user's invocation would hit.

**Bounded translation.** The projector translates a deliberately limited argparse subset:
scalar `str` / `int` / `float`; `store_true` / `store_false` as booleans; `choices` as
enums; `nargs` and repeated values as arrays, with bounds where mechanically known;
requiredness, defaults, and aliases; positionals in parser order; mutually exclusive groups.

**Unrepresentable means unavailable, never degraded.** An unknown `Action` subclass, a
callable `type=` the projector cannot model, or an ambiguous nested subparser makes that
verb unavailable with a schema-generation error naming the offending action. Silently
coercing an unmodelable parameter to `string` would produce a schema that lies, which is
worse than an absent verb.

**Playbooks resolve in two stages.** `li play` rewrites into an `orchestrate flow`
invocation, and a playbook's declared arguments are injected into the parser only after the
playbook name is known (`inject_playbook_schema_into_parser`). A playbook-bearing verb
therefore cannot have one static schema:

1. Base help exposes the playbook parameter, the prompt, the common flow arguments, and the
   fact that playbook-defined arguments exist.
2. Help naming the playbook resolves it, performs the same injection the CLI performs, and
   returns the resulting schema plus a fingerprint of the resolved playbook.
3. Execution repeats the resolution and validates against the playbook as it is *then*. A
   fingerprint that changed between discovery and execution is surfaced **in the result the
   caller receives**, not written to a log the caller never reads.

That last point carries more weight than its size suggests. The failure it prevents is an
artifact on disk changing after the step that validated it, leaving the executor unable to
tell that what it ran is not what was checked. A caller that validated against one version
of a playbook and executed against another must be told so where it will actually see it.

A static union over every installed playbook is rejected: it is stale the moment a playbook
is edited, and it reintroduces exactly the schema-size problem D2 exists to prevent.

**`extra_args` is not accepted anywhere on this surface.** Structured parameters are closed
and typos are rejected; there is no opaque argv channel alongside them.

The escape hatch existed to reach flags the schema did not model. D2 and D3 remove the
premise: the schema is projected from the parser the CLI itself builds, so a flag that exists
is a flag the schema models, and one that is not projectable is unavailable by decision rather
than reachable by accident. What remains of the hatch is only its cost.

That cost is a fence obligation. D8 is an allowlist, and an argv channel is a second way into
the same executable that the allowlist does not describe. Keeping it means every release must
re-establish that no sequence of opaque tokens introduces a new command boundary — a proof
about string parsing that has to hold forever and is one argparse behaviour change from being
wrong. Deleting the channel discharges the obligation instead of re-proving it. This is not a
hypothetical class: a caller value reaching argv's option position was found and fixed on the
implementing change, in the rendering of *typed* parameters, where the schema is known.

The concrete loss is one real use: passing a prompt file as `["--prompt-file", "/abs/path"]`
because no typed parameter existed. That becomes a typed `prompt_file` parameter, read and
snapshotted at submit time, which is strictly better than the argv form it replaces — the
caller gets validation and a defined read moment rather than a flag appended to a command
line. It lands with the same change that removes the hatch, on every spawn verb.

**Why this is written down rather than left as an implementation choice.** A future reader
comparing the code to an ADR that still declared an escape hatch would restore it to match,
and reopen the argv path the fence does not describe. The reasoning is the load-bearing part.

**Why runtime rather than the alternatives.** A build-time artifact cannot model
user-installed or user-edited playbooks, so it proves only the source tree and not the
executing environment — it fails the "live" requirement it is meant to satisfy. A
declarative registry would eventually be cleaner and would make schema evolution reviewable
without executing parser construction, but it converts a control-plane addition into a
CLI-wide migration touching every parser and handler, and v2 should not depend on it. The
accepted cost is that the projector reads argparse internals that are not a supported API;
that risk is contained to one module and pinned by golden projection tests per verb, so an
argparse change fails a test rather than silently corrupting a schema.

### D4 — Dispatch executes by subprocess, gated on a machine-result contract

Every dispatched verb runs by invoking the resolved `li` executable as a subprocess
(`config.li_command()`, `shell=False`). There is **no in-process fast path for read-only
verbs** in v2.

The tempting split — in-process for reads, subprocess for writes — is rejected because
mutability is not where the drift lies. A read still carries parser defaults, project and
cwd resolution, settings resolution, permission behavior, and error semantics. Calling
Python entry points directly would create a second path through all of that, and the two
paths would diverge silently, which is precisely the failure a thin control plane exists to
avoid.

**The admission rule is not read-only versus mutating. It is:**

> A verb is MCP-reachable only if its canonical `li` path accepts machine input through the
> projected parser **and emits a versioned machine result**, without any scraping of human
> output.

Many commands today print prose for a human reader. For those, the fix belongs in the CLI:
the command gains a canonical machine-output seam (a handler result object serialized
through one shared adapter). Until that seam exists, the verb is simply absent from the
allowlist and `help` says why. The MCP layer must never regex or heuristically parse console
text — that would turn a wording change into an API break, and it would violate D7's raw-JSON
requirement at the source.

Each operation captures bounded stdout and stderr, requires exactly one JSON value on the
machine channel, and maps the outcome into D7's per-op envelope. Launch failure, invalid or
absent JSON, output overflow, and a nonzero exit without a valid error object are each
explicit operation errors, never silent successes.

**Discovery does not confer authorization.** The projector can generate a schema for any
parser it can read; that does not make the verb reachable. The allowlist is separate,
explicit, and strictly narrower than what discovery can see, so adding a CLI command never
silently widens the MCP surface (see D8).

**The accepted cost** is one process start per dispatched call, which is acceptable for a
control plane. If measurement later shows startup dominating batch throughput, the answer is
a shared service layer that both the CLI and the MCP project onto — not human-text parsing
and not a second in-process path.

### D5 — Kill delegates entity-id semantics to `li kill`

v1's `job_kill` signals the detached process group directly
(`os.killpg(os.getpgid(pid), sig)`). That is correct for a single `li agent` job, where the
detached child leads its own process group and the whole tree shares the pgid.

It is not sufficient once `play.submit` exists. `li kill` performs transitive
`play → session → invocation` reaping, implied for plays without `--recursive`
(`cli/kill.py:246` resolves the play's session via `plays.session_id`, `:258` recurses into
that session's children, `:287-306` walks the frontier transitively, `:491` invokes the walk
for plays with no `recursive` guard). That path also writes the lifecycle status transitions
and fires the terminal-notify emit. A raw `killpg` stops the processes while leaving those
rows un-transitioned and skipping the notify.

**Decision:**

- When the job record carries a durable entity identity (play, session, invocation), the
  kill verb delegates to `li kill <entity_id>`, so lifecycle transitions, partial-reap
  reporting, reason codes, and the terminal-notify emit are inherited rather than
  re-implemented.
- `killpg` applies **only** to a record with no durable entity identity, where the process
  group the MCP itself created is the only thing there is to signal.
- If an entity-aware kill fails, times out, or reports a partial reap, that structured
  outcome is returned. **There is no `killpg` fallback after a known-entity kill fails.**

That last point is the important one. A failed entity kill is evidence that lifecycle-safe
termination did not complete; reaching for raw process termination at that moment would stop
the processes while leaving the lifecycle rows claiming otherwise — reproducing the exact
state/process split ADR-0104 was written to eliminate. A visible, reconcilable failure is
strictly better than an invisible divergence.

Consequently, `play.submit` and every new spawn path must **persist the entity identity**
needed for a lifecycle-safe kill, not merely the pid and the MCP `run_id`. The sidecar may
cache that identity as process bookkeeping; it must not invent lifecycle status.

Calling the CLI kill module's async internals in-process is rejected: the separate process
performs the normal settings and bootstrap registration that the terminal-callback emit
depends on, and the CLI is already resolved by the same absolute-path logic the submits use,
so delegation adds no new dependency.

**Known gap, inherited deliberately:** show-level reaping is deferred — `li kill <show_id>`
marks the show row terminal and does not reap its plays or their workers
(`cli/kill.py:493-503`). The MCP surface therefore does **not** expose a show-level kill. A
verb that silently half-stops a tree is worse than no verb; if a show-level operation is
added later, it must either recurse properly or fail explicitly.

### D6 — `job.wait` is a bounded observation with partial results

`job.wait` takes ids, a maximum wait, and a poll interval. The server clamps both numbers to
documented bounds and echoes the effective values back:

```json
{"ids": ["..."], "max_wait_seconds": 25, "poll_interval_seconds": 1}
```

The default maximum sits conservatively below ordinary client timeouts; `0` is a legal
snapshot request.

**Every successful call returns one entry per requested id, in input order**, carrying that
id's kind, label, status, whether it is `terminal`, its `outcome`, its `reason_code`, and
whether its process is `possibly_orphaned` — plus `all_terminal`, `timed_out`, and the lists
that account for the ids still unresolved. Every entry carries the same keys whether or not
it also carries an `error`, so a caller reads one shape rather than two.
Returning a bare boolean is rejected: mixed outcomes (two children done, one failed, one
running) are the normal case, and collapsing them forces an immediate follow-up poll that
the call was supposed to replace. `outcome` rides beside `terminal` rather than being left
to the caller, because status is an open vocabulary: a caller deriving success from it needs
a copy of our status names, and a copy goes stale silently in the direction of reading an
unrecognised failure as a success.

**The result partitions the observed ids, and the partition is exhaustive.** Every id that
was observed without a per-id error is exactly one of: terminal; still `pending`, meaning
further waiting can still change its answer; in `stopped_without_end`, meaning the record
stopped or cannot be shown to be progressing — its process gone with no end recorded, a
conclusive finding whose transition could not be published, or a record with no recorded
spawn phase — and waiting cannot change it; or in `unresolved_spawn`, meaning the record
still says `preparing` past the producer's stated age threshold (echoed in the result), so
there is no process to prove absent and waiting has stopped meaning anything. The four are
disjoint and together cover
every observed id, so a caller can hold a policy for each and know it has covered them all.
An id that is not resolved and is not named anywhere is a defect in the implementation, not
a caller's problem to infer: the duty to have a policy for an unresolved run is only
dischargeable if every unresolved run arrives somewhere it can be read. A future
non-terminal classification is therefore added to a list at the same time it is added to the
classifier, never to the classifier alone.

Because an id in `stopped_without_end` or `unresolved_spawn` resolves nothing by waiting, a
caller looping until
`all_terminal` would otherwise re-ask as fast as it could send. So a call that would return
without having waited at all, while at least one id is in either list, first waits one poll
interval — bounded by whatever is left of the window — and observes again. This is a floor
on the call rather than a charge added to it: a call that already waited on a running id has
met it, and a snapshot request has no window to spend and pays nothing. Pacing sits here
because the boundary applies it once for every client, where a documented duty to back off
binds only the clients that read it.

**Expiry is not an error.** A timed-out wait means the observation window closed, nothing
more. It reports what was learned, so completed children are not discarded and a retry is
safe. Unknown or ambiguous ids are per-id errors inside the result and never prevent the
other ids from being observed. Those errors are a separate channel from the pending list and
do not define it: they are for ids that could not be observed at all, so an id that *was*
observed is placed by the partition above rather than by what the error channel excludes.

**Observing does not touch the run — with one deliberate, fenced exception.** A wait that
expires, that is signalled, or whose caller
disconnects leaves the durable record exactly as it was. The exception: the first reader of
a `started` run whose process is conclusively established gone may durably reap it to an
attributable terminal where the fenced write publishes; a refused write leaves the record
exactly as every other observation does (ADR-0106 D10 and ADR-0107 specify the evidence and
the fence). Cancelling an observation is not
cancelling the work, and no implementation may make the two the same operation — a caller
that walks away from a bounded wait has said nothing about whether the work should continue,
and the only safe reading of silence is that it should.

**Lifecycle state is the single authority.** Terminal status and reason come from the same
cross-kind resolver `li wait` uses, not from the MCP job sidecar. The sidecar is
authoritative for its own narrow job bookkeeping, but `job.wait` accepts ids the MCP never
submitted, and a universal verb cannot pick its source of truth based on who submitted the
work — the same id would then answer differently depending on provenance. Sidecar data
(pid, console path, notify-delivery outcome) may ride along as auxiliary metadata; it never
overrides lifecycle status.

**`job.wait` and terminal-notify are complementary, not competing.** Use notify when the run
is expected to outlive a normal call or when the caller can receive a push. Use `job.wait`
for a bounded synchronization point, to join several children, or to reconcile after a
notification that never arrived. A notification is a prompt to read state, not proof of
terminal state.

Holding an MCP request open indefinitely is rejected as the primitive for long work. If
durable protocol-native task handles become available, they replace this bounded call
rather than being simulated by it.

### D7 — Response conventions

These apply uniformly to every verb.

- **Raw machine JSON.** No humanized fields — no relative timestamps ("2 minutes ago"), no
  pretty-printed durations, no formatted tables. Every consumer of this surface is a
  program; a humanization layer silently corrupts machine consumers that parse what they
  are given.
- **Closed argument validation.** An unknown or misspelled parameter is rejected loudly,
  echoing the offending name back. Silently ignoring an unrecognized argument turns a typo
  into a wrong-but-successful call. There is no `extra_args` escape hatch beside it: per D3
  the schema is projected from the parser, so a flag the schema does not model is one the
  surface does not reach.
- **Per-op error envelope.** Each op returns `{ok, op, ...}`; a failing op returns
  `{ok: false, op, error}` carrying that verb's expected schema per D2. The outer call returns an overall `status` of `success` or
  `partial` and **never throws for a per-op failure**. Callers check per-op `ok`.
- **Batch with an explicit cap.** `ops` accepts multiple entries from day one, with a
  documented maximum. Exceeding it is an explicit error, never a silent truncation.

### D8 — Visibility fence for privilege-granting operations

`state migrate`, `plugin trust`, and `hooks trust` are **not** reachable from the MCP
surface — not as verbs, not through any `ops` entry.

MCP callers are agents. "An agent may mark a plugin as trusted" is self-authorizing
privilege escalation: the thing being granted trust and the thing granting it are the same
actor. Schema migration is excluded on the same principle — it rewrites the state store the
rest of the surface reports on. These remain human-at-a-terminal operations.

The fence is an allowlist, not a denylist: a verb is reachable only if it is explicitly
registered, so a newly added CLI subcommand is unreachable until someone decides otherwise.

### D9 — The MCP stays standalone

The MCP surface remains part of lionagi under `li mcp`. It does not become a plugin of, or
a proxy for, another tool's dispatch surface, and nothing outside lionagi gains a second
write path into `state.db`.

A second writer would have to mirror every semantic of the first — lifecycle transitions,
terminal floor, CAS guards, audit rows — and fail closed on every error dimension, forever.
The integration point for other tools is the terminal-notify callback (data flows out;
control stays in lionagi), which already exists and requires no coupling.

## Implementation gates

These are measurements and seams the decisions above depend on. Each is a gate, not a
follow-up.

1. **Parser inventory.** ✅ **Satisfied 2026-07-25.** Measured: 316 argparse actions across
   70 parser paths in 18 command groups, every group building without error. Of those
   actions, none use an action class outside D3's bounded subset (0.0%), and one uses a
   non-scalar `type=` callable (0.3%). The gate's threshold was materially more than a
   tenth; the measurement is two orders of magnitude below it, so D3 stands and the
   declarative-registry alternative does not need re-examination.

   One implementation constraint fell out of the measurement: the per-command
   `parser_factory` return values are **not** uniform — `orchestrate` returns a dict where
   others return a parser — so the projector must walk the root parser's registered
   subparsers action rather than whatever the factory hands back. Walking factory returns
   covers 16 of the 70 paths and raises on `orchestrate`.
2. **The shared machine-result seam.** D4 admits a verb only once its CLI path emits a
   versioned machine result. That seam is CLI work that precedes the allowlist, and it may
   be larger than v2's other parts. It is deliberately not compensated for with human-text
   parsing.
3. **Durable entity identity on submit records.** D5's kill delegation requires it, so it
   lands before `play.submit` is promoted.
4. **Response-size ceiling test.** D2's selectivity needs a test that fails if the
   advertised tool schema grows past a fixed bound.

## Consequences

- The tool list stays at exactly one, regardless of how much of the CLI becomes reachable.
  Client-side tool-selection cost and advertised-schema cost both stay flat as coverage
  grows, rather than growing with it.
- `request(help=true)` becomes load-bearing documentation. If schema generation breaks, the
  dispatch surface becomes undiscoverable — so its generation needs a test that fails when
  the parser internals it reads change shape.
- `play.submit` and `job.wait` close the two largest gaps: playbooks were unreachable, and
  completion was pollable but not waitable.
- Kill delegation means the MCP inherits `li kill`'s semantics including its deferrals. The
  show-level gap is documented rather than papered over.
- The visibility fence means some CLI capability is permanently out of reach from MCP. That
  is the intent, not a limitation to be lifted later without a decision.
- **Tool-name-keyed policy loses its signal, and this is the largest hidden cost of the
  shape.** External layers that gate, audit, or annotate MCP calls commonly key on the tool
  name: permission prompts, allowlists, audit filters, and hooks that rewrite arguments
  before a call. Against a surface with one tool, the name no longer distinguishes reading
  a job's status from spawning twenty agents, so every such layer must either inspect
  `ops[].op` or treat the whole surface as one undifferentiated capability. Anything that
  cannot be made payload-aware degrades to all-or-nothing.

  This is a real and permanent trade, not a migration detail. It is accepted here because a
  payload-aware check is strictly more precise than a name match — it can distinguish verbs
  that a name match never could, since one tool name was already covering several
  operations — and because the alternative preserves the signal only by paying the
  advertised-schema cost this decision exists to remove. Adopters of this shape should
  expect to rewrite tool-name-based policy before cutover, not after: a policy layer that
  silently stops matching does not fail loudly, it fails open.

## Alternatives considered

**One discrete tool per CLI subcommand (~60 tools).** Rejected: a tool list that large
degrades client tool-selection, and every CLI addition becomes an MCP change. The dispatch
verb absorbs growth without a surface change.

**A discrete core of high-frequency tools alongside the dispatch verb.** This was the
original decision here, and it was reversed. The case for it was that constantly-used
operations deserve first-class, individually-advertised signatures rather than a layer of
indirection on the hot path.

It was rejected on measurement and on consistency. The advertised schema for that core is
paid on every request of every session by every caller, and the three submit verbs alone
account for 81% of the v1 server's 8,683 advertised bytes. More decisively, D2 requires
on-demand resolution because schemas are expensive, and a discrete core exempts the most
expensive schemas from precisely that rule. The hot-path concern is real but is answered by
D2's catalog signatures and schema-bearing rejections rather than by permanent advertisement,
and single-tool surfaces at several times this verb count show the ergonomic holds.

**A single hot verb kept discrete (two tools).** Rejected: it reopens the exemption argument
for every verb that later becomes frequent. Uniformity is the property being bought.

**A string-DSL escape hatch alongside JSON dispatch.** Rejected: the payloads are free-text
prompts; a DSL makes every call a quoting problem and needs hand-rolled escaping for text
the client can already express as JSON.

**Hand-maintained parameter documentation.** Rejected: it goes stale, and a stale schema on
a dispatch surface is worse than none because callers trust it.

**Publishing every reachable verb's schema in the tool's advertised parameters.** Rejected
under D2: the payload is sent on every request and can grow past what a client accepts,
failing the whole tool rather than one verb. Discovery is a call.

**In-process execution for read-only verbs.** Rejected under D4: reads carry the same
parser, settings, project-resolution, and permission semantics as writes, so a second
in-process path would drift silently from the CLI it is supposed to mirror.

**Parsing human console output to synthesize JSON.** Rejected under D4: it makes prose
wording an API contract. A command without a machine-result seam stays unreachable until it
has one.

**Falling back to `killpg` when an entity-aware kill fails.** Rejected under D5: it converts
a visible, reconcilable lifecycle failure into a silent process/state divergence — the exact
condition ADR-0104 removed.

**Selecting `job.wait`'s source of truth by provenance** (sidecar for MCP-submitted jobs,
lifecycle state otherwise). Rejected under D6: the same id would answer differently
depending on who submitted it.

**Proxying the surface through another tool's dispatch layer.** Rejected under D9: a second
write path into `state.db` must mirror all semantics forever.
