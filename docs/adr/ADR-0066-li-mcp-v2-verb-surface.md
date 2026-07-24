# ADR-0066: `li mcp` v2 verb surface — discrete core plus one generated dispatch

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: cli-surface
- **Date**: 2026-07-24
- **Relations**: builds on ADR-0095 (run-terminal callbacks — the `notify.on_terminal`
  layer the MCP submits ride) and ADR-0104 (`li kill` transitive play reaping and
  terminal-notify on kill, whose semantics the kill verb must inherit rather than
  re-implement); none superseded

## Depth contract

This ADR decides the shape of the MCP verb surface, not its implementation schedule. The
surface measurements below were taken from the built parsers at commit `44ec77901`, and the
kill semantics in D5 were verified by source read of `cli/kill.py` at the same commit, not
from the prose of the ADR that introduced them. Where a claim depends on an existing
contract, the contract is named with its file.

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

### D1 — Discrete core, one dispatch verb for the long tail

Keep discrete tools for the high-frequency operations, and add exactly one dispatch verb for
everything else.

Discrete core (10 tools):

- `submit_agent`, `submit_flow`, `submit_fanout` — unchanged from v1.
- `submit_play` — new. Playbooks are a first-class spawn surface (`li play <name>`) and are
  used as heavily as flows; leaving them unreachable forces callers to hand-expand a
  playbook into a flow invocation.
- `wait` — new. See D6.
- `job_status`, `job_output`, `job_kill`, `jobs_list` — unchanged from v1.

Long tail (one tool): `request(ops=[{tool, args}], help?)`, covering `schedule_*`, `team_*`,
`monitor`, `stats`, `state` (read-only subset), `dispatch`, and future additions.

**Dispatch is JSON-form only.** `ops` is a list of objects, never a string to be parsed:

```json
{"ops": [{"tool": "schedule_list", "args": {"limit": 20}}]}
```

A string-DSL escape hatch is explicitly rejected. The payloads this surface carries are
prompts — arbitrary text with quotes, newlines, and braces — and any DSL would require
hand-rolled escaping at every call site for no gain over JSON the client already speaks.

**Why both.** Discrete tools give the operations a caller uses constantly a first-class,
individually-documented signature. The dispatch verb keeps the tool list small and bounded
while the reachable surface grows with the CLI. The split point is frequency, not
capability.

### D2 — `help` returns a live, parser-derived schema, resolved on demand

`request(help=...)` returns parameter schemas **generated from the CLI parsers at runtime**,
never a hand-maintained table.

Documentation lags the code it documents; a generated schema cannot. This is the single
convention that keeps a dispatch surface honest as the CLI moves underneath it, and it is
the reason a dispatch verb is acceptable at all: without a live schema, `request(...)`
would be an undiscoverable string interface.

**Help is selective, and the schema is never published eagerly.** With no target, `help`
returns the allowed-verb catalog with compact one-line summaries. With a verb named, it
returns that verb's full schema. The MCP tool's own advertised schema stays small: it
describes `ops` and `help`, not the union of every reachable verb's parameters.

This is a hard constraint, not a preference. Flattening a large parameter surface into a
single advertised schema is a known failure mode — a schema that grows into the tens of
kilobytes is sent to the model on every request and can exceed what a client will accept,
which takes down the whole tool rather than degrading one verb. Discovery must therefore be
a call, not a payload.

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
   fingerprint that changed between discovery and execution is reported, not ignored.

A static union over every installed playbook is rejected: it is stale the moment a playbook
is edited, and it reintroduces exactly the schema-size problem D2 exists to prevent.

**`extra_args` coexists with closed validation as a declared escape hatch.** Structured
parameters stay closed and typos are rejected. `extra_args` is opaque argv by contract, is
logged with secrets redacted, and marks its result as having bypassed validation. It is
**not** accepted by the generic dispatch verb, and on the submit verbs it is checked so it
cannot introduce a new command boundary — otherwise the D8 fence would be bypassable
through argv.

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

It is not sufficient once `submit_play` exists. `li kill` performs transitive
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

Consequently, `submit_play` and every new submit path must **persist the entity identity**
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

### D6 — `wait` is a bounded observation with partial results

`wait` takes ids, a maximum wait, and a poll interval. The server clamps both numbers to
documented bounds and echoes the effective values back:

```json
{"ids": ["..."], "max_wait_seconds": 25, "poll_interval_seconds": 1}
```

The default maximum sits conservatively below ordinary client timeouts; `0` is a legal
snapshot request.

**Every successful call returns one entry per requested id, in input order**, carrying that
id's kind, status, whether it is terminal, and its reason code — plus `all_terminal`,
`timed_out`, and the list of ids still pending. Returning a bare boolean is rejected: mixed
outcomes (two children done, one failed, one running) are the normal case, and collapsing
them forces an immediate follow-up poll that the call was supposed to replace.

**Expiry is not an error.** A timed-out wait means the observation window closed, nothing
more. It reports what was learned, so completed children are not discarded and a retry is
safe. Unknown or ambiguous ids are per-id errors inside the result and never prevent the
other ids from being observed.

**Lifecycle state is the single authority.** Terminal status and reason come from the same
cross-kind resolver `li wait` uses, not from the MCP job sidecar. The sidecar is
authoritative for its own narrow job bookkeeping, but `wait` accepts ids the MCP never
submitted, and a universal verb cannot pick its source of truth based on who submitted the
work — the same id would then answer differently depending on provenance. Sidecar data
(pid, console path, notify-delivery outcome) may ride along as auxiliary metadata; it never
overrides lifecycle status.

**`wait` and terminal-notify are complementary, not competing.** Use notify when the run is
expected to outlive a normal call or when the caller can receive a push. Use `wait` for a
bounded synchronization point, to join several children, or to reconcile after a
notification that never arrived. A notification is a prompt to read state, not proof of
terminal state.

Holding an MCP request open indefinitely is rejected as the primitive for long work. If
durable protocol-native task handles become available, they replace this bounded call
rather than being simulated by it.

### D7 — Response conventions

These apply uniformly to every verb, discrete and dispatched.

- **Raw machine JSON.** No humanized fields — no relative timestamps ("2 minutes ago"), no
  pretty-printed durations, no formatted tables. Every consumer of this surface is a
  program; a humanization layer silently corrupts machine consumers that parse what they
  are given.
- **Closed argument validation.** An unknown or misspelled parameter is rejected loudly,
  echoing the offending name back. Silently ignoring an unrecognized argument turns a typo
  into a wrong-but-successful call. `--extra-args` remains the documented escape hatch for
  passing through flags the schema does not model, and its use is logged.
- **Per-op error envelope.** Each op returns `{ok, tool, ...}`; a failing op returns
  `{ok: false, tool, error}`. The outer call returns an overall `status` of `success` or
  `partial` and **never throws for a per-op failure**. Callers check per-op `ok`.
- **Batch with an explicit cap.** `ops` accepts multiple entries from day one, with a
  documented maximum. Exceeding it is an explicit error, never a silent truncation.

### D8 — Visibility fence for privilege-granting operations

`state migrate`, `plugin trust`, and `hooks trust` are **not** reachable from the MCP
surface — not as discrete verbs, not through `request(...)`.

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

1. **Parser inventory.** Before committing to D3's bounded subset, inventory the argparse
   actions across the candidate verbs. If materially more than a tenth of them need
   semantics the bounded projector cannot represent, D3's cost/benefit changes and the
   declarative-registry alternative deserves re-examination.
2. **The shared machine-result seam.** D4 admits a verb only once its CLI path emits a
   versioned machine result. That seam is CLI work that precedes the allowlist, and it may
   be larger than v2's other parts. It is deliberately not compensated for with human-text
   parsing.
3. **Durable entity identity on submit records.** D5's kill delegation requires it, so it
   lands before `submit_play` is promoted.
4. **Response-size ceiling test.** D2's selectivity needs a test that fails if the
   advertised tool schema grows past a fixed bound.

## Consequences

- The tool list stays at 10 discrete verbs plus `request`, regardless of how much of the
  CLI becomes reachable. Client-side tool-selection cost stays flat as coverage grows.
- `request(help=true)` becomes load-bearing documentation. If schema generation breaks, the
  dispatch surface becomes undiscoverable — so its generation needs a test that fails when
  the parser internals it reads change shape.
- `submit_play` and `wait` close the two largest gaps: playbooks were unreachable, and
  completion was pollable but not waitable.
- Kill delegation means the MCP inherits `li kill`'s semantics including its deferrals. The
  show-level gap is documented rather than papered over.
- The visibility fence means some CLI capability is permanently out of reach from MCP. That
  is the intent, not a limitation to be lifted later without a decision.

## Alternatives considered

**One discrete tool per CLI subcommand (~60 tools).** Rejected: a tool list that large
degrades client tool-selection, and every CLI addition becomes an MCP change. The dispatch
verb absorbs growth without a surface change.

**A single dispatch verb with no discrete tools.** Rejected: the operations used constantly
(the submits, job status) deserve first-class signatures, and forcing them through a generic
envelope adds a layer of indirection to the hot path for uniformity's sake.

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

**Selecting `wait`'s source of truth by provenance** (sidecar for MCP-submitted jobs,
lifecycle state otherwise). Rejected under D6: the same id would answer differently
depending on who submitted it.

**Proxying the surface through another tool's dispatch layer.** Rejected under D9: a second
write path into `state.db` must mirror all semantics forever.
