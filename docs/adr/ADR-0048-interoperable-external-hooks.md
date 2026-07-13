# ADR-0048: Interoperable external hooks (Claude Code / Codex hook contract)

- **Status**: Accepted
- **Kind**: Aspirational
- **Area**: hooks
- **Date**: 2026-07-12
- **Relations**: extends ADR-0047 (hook mechanism scopes — this ADR adds an external,
  cross-harness contract on top of the mechanisms ADR-0047 ratified; it does not move
  their ownership boundaries); builds on ADR-0095 (its D3 no-shell executable-adapter
  posture is adopted verbatim for hook commands)

## Context

LionAGI has three in-process hook mechanisms with distinct scopes, ratified by
ADR-0047: the session-scoped `HookBus` (`lionagi/hooks/`, observe/audit plane with one
hard-wired blocking point at `TOOL_PRE`), the tool-scoped preprocessor/postprocessor
chain (`lionagi/agent/spec.py` `HooksMixin`, full-payload mutation), and the
service-scoped `HookRegistry` on `iModel` events. All three register in-process Python
callables. The only way to run an *external program* as a hook today is
`lionagi/agent/settings.py`'s `_make_shell_hook`: argv-only subprocess, stdin JSON,
fixed 10-second timeout, and — critically — no stdout read-back. An external hook can
veto (nonzero exit becomes `PermissionError` on the pre phase) but cannot rewrite
arguments, attach context, or return a structured decision.

Meanwhile the two dominant agent harnesses ship external-hook contracts of the same
*family*: a JSON envelope on stdin carrying `session_id`, `cwd`, `hook_event_name`,
and per-event fields (`tool_name`, `tool_input`, `tool_response`); an exit-code
protocol (0 = success and stdout is parsed as JSON, 2 = block with stderr as the
reason, other = non-blocking failure); a stdout decision shape
(`hookSpecificOutput.permissionDecision` with `permissionDecisionReason` and optional
`updatedInput` for `PreToolUse`; top-level `decision: "block"` + `reason` for most
other events); and a nested config shape (`hooks.<EventName>` → `[{matcher, hooks:
[{type, command, timeout}]}]`) with largely shared event names. The family
resemblance is real, but they are **not one contract**, and this ADR must not
pretend otherwise. Divergences that matter here: Claude Code configures `command`
as a shell string (with a separate args-array form for no-shell execution) while
Codex executes the configured command as shell text; Codex's `UserPromptSubmit`
input schema *requires* fields (`model`, `permission_mode`, `transcript_path`,
`turn_id`) that Claude Code does not; Codex's `PreToolUse` decision vocabulary
admits only `allow|deny|ask`; and Claude Code cancels a timed-out
`UserPromptSubmit` hook and lets the prompt proceed, i.e. fails open. Codex
additionally ships a hash-pinned trust gate: a non-managed hook's exact command
must be explicitly trusted before it runs.

The shared family is still an opportunity with a deadline attached. Users who
already maintain a hardened hook suite for Claude Code or Codex (guards,
formatters, audit loggers, notification bridges) should be able to bring most of it
to LionAGI, and hooks written against the common subset should travel the other
way. If LionAGI invents an unrelated third dialect, every hook gets written twice
and the ecosystem's existing hook tooling is unusable here. But because the two
harnesses genuinely differ, the deliverable is a **stated, versioned compatibility
profile** — exactly which events, fields, decisions, and config forms LionAGI
guarantees, and where it knowingly diverges from each harness — not a claim of
equivalence the upstream systems themselves do not have.

Named problems:

- **P1 — no structured decision channel.** `_make_shell_hook` inspects only
  `returncode`/stderr. An external guard cannot say "allow but rewrite the argument,"
  "deny with this machine-readable reason," or "attach this context to the turn."
  Every richer behavior currently requires an in-process Python hook, which
  cross-harness users cannot share.
- **P2 — no cross-harness config portability.** LionAGI's `hooks:` settings shape
  (`{pre,post,on_error}: {tool_name: [spec]}`) is structurally unrelated to the
  CC/Codex shape (`hooks.<EventName>` → matcher groups). A team running both harnesses
  maintains two disjoint configurations for the same guard commands.
- **P3 — event-vocabulary gap.** CC/Codex hooks fire on `UserPromptSubmit`; LionAGI
  has no hook point that models "an instruction is about to be submitted to the
  model." A prompt-hygiene or context-injection hook has no seam to attach to.
- **P4 — seam mismatch on tool events.** The blocking point LionAGI exposes on
  `HookBus` (`TOOL_PRE`) carries a 200-character argument summary; honoring
  `updatedInput` requires the full-payload tool preprocessor chain. Those tool hooks
  in turn do not fire for MCP-discovered tools at all today — the chain is wired per
  registered `Tool` at agent-factory time, and MCP server tools registered through
  `ActionManager.register_mcp_server` bypass it. An external `PreToolUse` hook that
  silently skips MCP tools is a security hole, not a feature.
- **P5 — no trust boundary for command hooks.** `settings.py` gates Python
  import-path hooks behind `trusted_hook_modules`, but shell-command hooks from any
  merged settings file run unconditionally. Once configs can be *imported* from
  `.claude/settings.json` or plugin bundles (ADR-0088), "whatever the file says,
  execute it" is not a defensible posture.

| Concern | Decision |
|---------|----------|
| External wire contract | D1: a versioned LionAGI compatibility profile over the CC/Codex family — exact field guarantees and named divergences, no equivalence claim |
| Event vocabulary and mapping | D2: fixed mapping table; `USER_PROMPT_SUBMIT` added with a turn-origin token consumed exactly once; unmapped events fail loud at load |
| Which internal seam serves tool events | D3: tool pre/post events route to the tool-hook chain, relocated to the `ActionManager` invoke chokepoint (Branch-mediated coverage incl. MCP; direct `FunctionCalling` construction is a documented, tested bypass) |
| Executor | D4: one exec adapter extending `_make_shell_hook` — non-empty argv only, stdout parse-back, per-hook timeout |
| Decision semantics | D5: `allow` continues, `deny` raises, `ask` fails closed, unrecognized decisions fail closed; `updatedInput` honored only at the preprocessor seam |
| Config surface | D6: CC-shaped `hooks_external:` block in `.lionagi/settings.yaml`, plus explicit import of CC/Codex hook configs with per-entry source provenance |
| Trust | D7: hash-pinned trust for command hooks that are not project-authored; provenance markers survive import |

Out of scope for this ADR:

- **HTTP / MCP-tool / prompt / agent hook handler types** (CC's `type: http|mcp_tool|
  prompt|agent`) — deferred; the D6 config schema reserves the `type` field so they
  can be added without a shape break, but v1 executes only `type: command`.
- **`Stop` / `PreCompact` / `PostCompact` events** — LionAGI's runtime has no
  turn-stop arbitration loop or context-compaction phase to attach them to. Mapping
  them before the runtime concept exists would violate the rule that a hook point
  ships with its emit site (see D2). They become mappable when the corresponding
  runtime surfaces exist.
- **Making `HookBus` blocking behavior per-config.** Blocking stays a closed,
  code-reviewed property of specific hook points, per ADR-0047's rationale that
  ordinary hooks are intentionally failure-isolated. This ADR adds one new blocking
  point (D2) through code review, not a configuration switch.
- **The plugin bundle format that can carry hook configs** — ADR-0088.

## Decision

### D1 — A versioned compatibility profile, not an equivalence claim

LionAGI defines **external-hook compatibility profile v1**: an explicit, versioned
statement of the envelope LionAGI emits, the decisions it accepts, and how foreign
configurations translate. The profile follows the CC/Codex family shape wherever the
two harnesses agree, and **names every divergence** where they do not (or where
LionAGI deliberately departs). Nothing in this ADR claims the contract is verbatim,
converged, or that arbitrary foreign hooks run unmodified — those are properties the
conformance matrix (below) must demonstrate per event, not properties asserted by
adoption.

**Stdin envelope** (one JSON object, UTF-8, single line or pretty — the hook must
parse, not line-split):

```json
{
  "session_id": "s-…",
  "cwd": "/abs/path",
  "hook_event_name": "PreToolUse",
  "harness": "lionagi",
  "tool_name": "bash",
  "tool_input": {"command": ["git", "status"]},
  "tool_response": null
}
```

**Per-event field guarantees.** Common fields (`session_id`, `cwd`,
`hook_event_name`, `harness`) are always present. Per-event fields use the CC/Codex
field names, with an explicit presence guarantee each:

| Event | Field | Guarantee |
|---|---|---|
| `PreToolUse` | `tool_name` | always; the LionAGI registered name (`Tool.function`) — see the tool-naming divergence below |
| `PreToolUse` | `tool_input` | always; the LionAGI argument dict verbatim (already an arbitrary JSON-serializable mapping, no reshaping); a returned `updatedInput` replaces this dict whole — no per-key merge |
| `PostToolUse` | `tool_name`, `tool_input` | as above |
| `PostToolUse` | `tool_response` | always; the tool result as JSON where serializable, else its string form |
| `UserPromptSubmit` | `prompt` | always; the rendered instruction text actually being submitted |
| `UserPromptSubmit` | `model` | always; the branch's active model identifier |
| `UserPromptSubmit` | `permission_mode` | always; the attached `PermissionPolicy` mode, else `"default"` |
| all | `harness` | always `"lionagi"` — a hook that must behave differently per harness keys off this; CC and Codex omit it, so its absence means "not lionagi" |

**Named divergence Dv1-1 (envelope):** Codex's `UserPromptSubmit` input schema
additionally requires `transcript_path` and `turn_id`. LionAGI has no transcript
file or turn identifier on every surface and does not fabricate values: the fields
are present when the surface has them (a persisted run's branch-snapshot path; a
flow node id) and **omitted** otherwise. A Codex hook that hard-requires them does
not run unmodified on LionAGI; it must tolerate their absence when
`harness == "lionagi"`. The import command (D6) warns when it cannot prove an
imported hook tolerates this.

**Named divergence Dv1-2 (tool naming):** matchers and `tool_name` carry LionAGI
registered tool names, not CC/Codex built-in names (`Bash`, `Read`, …). No automatic
name mapping exists in v1 — a matcher written as `"Bash"` matches a LionAGI tool
only if a tool is registered under that exact name. `li hooks import` (D6) reports
every matcher that references a name with no registered LionAGI tool so the user
can re-target it; an alias table is future work that belongs to the tool registry,
not to this profile.

**Named divergence Dv1-3 (command form):** LionAGI executes argv vectors only —
never a shell (D4). CC configures `command` as a shell string (argv is a separate
form); Codex executes command text via a shell. Import translation (D6) tokenizes a
foreign command string only when it contains no shell metacharacters; anything else
is rejected-with-reason, never reinterpreted.

**Named divergence Dv1-4 (blocking timeout):** Claude Code cancels a timed-out
`UserPromptSubmit` hook and lets the prompt proceed — fail open. LionAGI fails
closed on every blocking point (D1 timeout rule below, D5). This is a deliberate
policy divergence: an unattended runtime must not admit an action its guard never
cleared. Hook authors porting from CC must know their timeout now blocks instead of
waving through.

**Exit-code protocol**, exactly as the harnesses define it:

- exit 0 — success; stdout is parsed as JSON if non-empty (parse failure of non-empty
  stdout is logged and treated as "no structured output," not as a block).
- exit 2 — block; stderr (trimmed) is the human-readable reason; stdout is ignored.
- any other exit — hook failure; logged with stderr; execution continues (a broken
  observer must not take down the run — same failure-isolation stance as
  `HookBus.emit`).
- timeout — the process group is terminated (`aterminate_process_group`, the existing
  teardown) and treated as the "other exit" case on advisory events, and as `deny`
  (fail closed) on blocking events. A guard that hangs must not admit the action it
  was guarding.

**Stdout decision shape** on exit 0: `hookSpecificOutput.permissionDecision` +
`permissionDecisionReason` + `updatedInput` for `PreToolUse`-mapped events;
`decision: "block"` + `reason` for other events. The accepted
`permissionDecision` vocabulary is the genuine cross-harness intersection —
`allow | deny | ask` (Codex's schemas admit nothing else) — with the exact
semantics in D5. Unknown *fields* are ignored (forward compatibility with harness
spec evolution); an unrecognized *decision value* fails closed as `deny` with a
diagnostic naming the value — a guard channel does not guess at verbs it does not
know.

**Conformance matrix (acceptance artifact).** The implementation ships a
conformance test matrix: for each profile event, fixture hooks authored against the
current CC documentation and the current Codex schemas run under LionAGI, asserting
each field-guarantee row and each named divergence above. Until that matrix exists
and passes, neither documentation nor `li hooks import` output may describe a
foreign hook as running "unmodified" — the import report says "imported; verify
against profile v1" and lists the divergences that apply to that entry.

Why this way: the alternative — a LionAGI-native contract with an adapter shim per
harness — was rejected because the CC/Codex family shape is expressive enough for
every P1–P5 need, and a third unrelated dialect creates permanent translation
liability for zero expressive gain. But the opposite temptation — declaring the
family a single converged contract and adopting it "verbatim" — fails on the facts:
the harnesses differ in command form, required input fields, decision vocabulary,
and timeout semantics, so an equivalence claim would leave every implementer to
rediscover the exceptions. A versioned profile with named divergences keeps the
portability of the shared shape and makes the residual differences a checkable
contract. The profile is LionAGI-owned and versioned here; when CC or Codex moves,
the profile revs with a new version entry in Notes, never silently.

### D2 — Event vocabulary: fixed mapping, fail-loud on the unmappable

The external event names LionAGI accepts in hook configuration, and the internal seam
each drives:

| External event | Internal seam | Capability |
|---|---|---|
| `SessionStart` | `HookPoint.SESSION_START` (HookBus) | observe; `additionalContext` ignored in v1 |
| `SessionEnd` | `HookPoint.SESSION_END` (HookBus) | observe |
| `UserPromptSubmit` | `HookPoint.USER_PROMPT_SUBMIT` (HookBus, **new, blocking**) | observe or block (exit 2 / `decision: "block"`) |
| `PreToolUse` | tool preprocessor chain at the invoke chokepoint (D3) | block, rewrite via `updatedInput` |
| `PostToolUse` | tool postprocessor chain at the invoke chokepoint (D3) | observe, annotate |
| `PostToolUseFailure` | `HookPoint.TOOL_ERROR` (HookBus) | observe (exception stringified into `tool_response.error`) |

Exact semantics:

- `USER_PROMPT_SUBMIT` is a new `HookPoint` enum member **shipped in the same change
  as its emit mechanism**. Placement alone cannot express "user-originated": the
  public ingress APIs are plural (`Branch.chat()` and `Branch.chat_and_record()`
  call the low-level chat operation directly, never entering `communicate`), and
  the internal callers of the same middles are also plural (ReAct drives repeated
  synthetic `operate()` calls through the very `communicate`/`run_and_collect`
  middles a middle-level emit would live in). Any fixed emit site is therefore
  either incomplete or over-broad. The mechanism is a **turn-origin token**:
  - **Created** at each public user-ingress API — `Branch.chat()`,
    `Branch.chat_and_record()`, `Branch.communicate()`, `Branch.operate()`,
    `Branch.run()`, and `Branch.ReAct()` — and carried on the operation context as
    an explicit field threaded through the call chain (not ambient task-local
    state, which would leak across concurrently running branch operations).
    Origination is **conditional on an explicit origin disposition** threaded on
    the same call chain, with three states: *unset* (the default a genuine
    outside caller produces), *forwarded token*, and *no-origin*. A public
    ingress mints a fresh token only when the disposition is *unset*; a
    forwarded token is carried through unchanged and never re-originated; and
    *no-origin* means the call traverses the public method without ever holding
    a token. This disposition — not the method being public — is the
    deterministic rule that distinguishes a user's call to `Branch.chat()` from
    the runtime's own call to the same public method.
  - **Nested public ingresses forward, never re-originate.** The in-tree nested
    path is named: `Branch.chat_and_record()` delegates to `Branch.chat()` — it
    mints (on *unset*) before delegating and passes that same token down as the
    *forwarded* disposition, so the delegated `chat()` mints nothing and the one
    token is consumed at the chat boundary as usual. Any future public wrapper
    that delegates to another public ingress inherits this forwarding rule.
  - **Consumed exactly once** at the model-submission boundary: immediately before
    provider invocation in the chat operation (which serves the chat,
    chat_and_record, communicate, and operate→communicate paths), and immediately
    before streaming begins in the `run` middle. Consumption is
    check-and-clear: if the token is present, `blocking_emit` fires with payload
    `{session_id, branch_id, prompt}` — `prompt` being the rendered instruction
    text actually submitted at that boundary — and the token is cleared for the
    remainder of the turn; if absent, the boundary stays silent.
  - **Internal turns pass the explicit *no-origin* disposition.** Instructions
    the runtime synthesizes are issued with *no-origin* at their named seams:
    parse-repair turns, where `parse._inner_parse()` calls the public
    `Branch.chat()` — the traversal of a public method with *no-origin* mints
    nothing — and ReAct extension/final-answer turns, which drive `operate()`
    directly with *no-origin*. Their submissions find no token and emit nothing.
    For a multi-step `ReAct()` call this means exactly one emission — at the
    first model submission of the user's turn — and silence for every internal
    continuation.
  - Adding the enum member without the token mechanism and both consuming
    boundaries is forbidden; ADR-0047 already documents four never-wired hook
    points as exactly this trap, and this ADR does not add a fifth.
- **Acceptance: exact-once, enumerated per path.** The implementation ships
  integration tests asserting the emission count for every ingress: direct
  `chat()` → 1; `chat_and_record()` delegating to `chat()` → 1 total (proving
  the forwarded disposition does not re-originate); `communicate()` → 1;
  `operate()` delegating to communicate → 1; direct `run()` → 1; a `ReAct()`
  call with multiple internal extension turns and a final-answer turn → exactly 1
  total; and a failing-then-repaired parse inside any of the above → exactly 1
  total, with the repair submission itself asserted as zero additional events —
  the row names its call path, `parse._inner_parse()` → `Branch.chat()` with
  *no-origin*. A new public
  ingress added later must add its row to this matrix — the test file carries a
  comment stating that rule.
- A blocked `USER_PROMPT_SUBMIT` surfaces as the same `PermissionError`-family
  failure the blocking convention already defines, at the operation boundary: an
  interactive session fails the turn with the hook's reason; a headless DAG node
  fails that node through the node's normal error path — never a silent skip,
  never a process abort.
- `USER_PROMPT_SUBMIT` becomes the second blocking point in `HookBus` (after
  `TOOL_PRE`). The blocking set remains hardcoded in `bus.py` — extending it is a
  code change with review, not configuration (see out-of-scope).
- A config that names any other external event (`Stop`, `PreCompact`,
  `SubagentStart`, `Notification`, …) **fails at config load** with a diagnostic
  naming the event and stating that LionAGI has no seam for it — never a silent
  drop. Rationale: a user who installs a stop-guard and gets no error believes they
  are protected; silent no-op on a guard is the worst failure mode available.
- Matchers follow the harness semantics: omitted/`""`/`"*"` matches all;
  alphanumeric/`_`/`-`/space/`,`/`|` strings are exact-or-list matches; anything else
  is an unanchored regex. The matched field is `tool_name` for tool events and the
  event's primary subject otherwise. Matching is evaluated by the adapter layer
  before spawning the process — a non-matching hook costs zero subprocesses.
- LionAGI-native hook points with no external counterpart (`BRANCH_CREATE`,
  `MESSAGE_ADD`, the service-scope `HookRegistry` events) are **not** exposed to
  external hook configs in v1. Exposing them would invent event names no other
  harness recognizes, recreating the portability problem this ADR exists to remove.
  They remain reachable by in-process hooks exactly as today.

### D3 — Tool events route through the invoke chokepoint

`PreToolUse`/`PostToolUse` adapters register into the tool preprocessor/postprocessor
chain, not `HookBus` — and that chain moves to `ActionManager.invoke`, the point
every **Branch-mediated** tool call passes through.

**Coverage, stated precisely.** The guarantee of this decision is scoped to tool
calls that flow through `ActionManager.invoke()`: the Branch action path
(`act`/`operate`/`ReAct` action requests) and every tool registered on the manager,
including MCP-discovered tools. That is the entire product surface LionAGI ships
today. It is **not** a universal interception point: `FunctionCalling` is itself an
executable event that runs `Tool.preprocessor`, the callable, and
`Tool.postprocessor` directly — code that constructs a `FunctionCalling` and
invokes it bypasses the manager layer, and always has. v1 does not internalize that
constructor (privatizing a long-public API is a breaking change with its own blast
radius, recorded below as deferred hardening); instead the bypass is a **named,
documented, tested limit**: an acceptance test constructs a `FunctionCalling`
directly and asserts external hooks do *not* fire, so the boundary is a pinned
contract rather than an accident, and code review owns keeping product paths on the
manager. Deferred hardening decision, recorded for a future ADR: make
`ActionManager` the sole constructor/invoker of `FunctionCalling` (or gate direct
construction behind an internal token), at which point the guarantee upgrades from
"Branch-mediated" to universal.

The contract:

- `ActionManager` gains an optional pre/post processor pair applied inside `invoke`,
  around the `Tool` call, for **every** tool invoked through it — plain function
  tools, `Tool` objects, and MCP-discovered tools alike. The existing per-`Tool` `preprocessor`/
  `postprocessor` attributes remain and run innermost (closest to the tool), so
  current `AgentSpec`/`HooksMixin` wiring keeps its behavior and ordering
  (`security -> user -> security recheck` is preserved within the existing layer).
- The external-hook adapter attaches at the `ActionManager` layer, outermost. Order
  on a call: external `PreToolUse` hooks (config order) → spec-level pre chain →
  tool → spec-level post chain → external `PostToolUse` hooks.
- The preprocessor receives and may replace the full argument dict (`updatedInput`);
  the postprocessor receives the full result. The postprocessor applies regardless of
  result type — the current dict-only restriction on the spec-level post chain is a
  known gap and is not inherited by the new layer.
- **Rewritten arguments are revalidated before the tool runs.** The current
  `FunctionCalling` path does not re-run request-model validation after a
  preprocessor replaces arguments (a gap ADR-0047 records). This ADR does not ship
  arg-rewrite on top of that gap: after the external hooks and the spec-level chain
  have both run, the final argument dict is validated against the tool's
  `request_options` (when the tool declares one) before the callable executes; a
  validation failure is a `deny`-equivalent block carrying the validation error.
  A tool without `request_options` runs on the rewritten dict as-is — that tool
  never had schema enforcement, and the external layer does not weaken or invent
  one.
- **`security_pre` stays the last pre-stage validator.** External hooks are
  strictly outside the spec-level chain, so any `updatedInput` rewrite happens
  before `security_pre` sees the arguments; the guard therefore always validates
  the post-rewrite values that will actually reach the tool. This holds in the
  no-user-hook case too (external rewrite, no spec-level user pre-hook: the single
  `security_pre` run still sees final args, because external ran first). The
  ordering is a load-bearing invariant of this design, not an accident — an
  implementation must not move external hooks inside or after the security stage.
- `HookBus.TOOL_PRE`/`TOOL_POST`/`TOOL_ERROR` continue to fire exactly as today
  (summary payloads, audit plane). D3 adds a mutation-capable layer; it does not
  repurpose the audit layer. A config-driven external hook therefore produces both
  its own effect and the ordinary `HookSignal` audit trail. One consequence to
  know: the bus's `TOOL_PRE` emit happens in the act layer before
  `ActionManager.invoke`, so its argument summary reflects the **pre-rewrite**
  arguments by construction. The faithful post-rewrite record lives in the tool
  event itself; if the audit plane ever needs the final args, that is a follow-up
  emit-site move, decided there, not silently here.

Why this way: the alternative — wiring external tool hooks into `HookBus.TOOL_PRE` —
was rejected because that point's payload is a truncated summary by design (the audit
plane must not hold full arguments), so `updatedInput` is unimplementable there, and
because MCP tools would remain uncovered. Moving enforcement to `ActionManager.invoke`
resolves the MCP gap for the external layer without touching the ADR-0047 ownership
boundaries: the bus stays the observe/audit plane, the tool chain stays the mutation
plane, and the chokepoint is simply where the mutation plane is anchored so coverage
is total.

### D4 — One exec adapter, extending the existing executor

A single adapter turns a hook config entry into an async callable conforming to the
target seam:

```python
def external_hook_adapter(
    *,
    event: str,                    # external event name, e.g. "PreToolUse"
    command: list[str],            # argv vector — never a shell string
    timeout: float = 60.0,
    matcher: str | None = None,
) -> HookHandler | ToolProcessor:  # shape depends on the mapped seam (D2/D3)
```

- The executor extends `_make_shell_hook`'s existing subprocess model —
  `asyncio.create_subprocess_exec`, stdin JSON write, bounded wait,
  `aterminate_process_group` on timeout — and adds what P1 requires: capture and
  parse stdout on exit 0, honor the D1/D5 decision semantics, distinguish exit 2 from
  other nonzero exits (the current executor collapses all nonzero to
  `PermissionError` on pre hooks; the new one reserves that meaning for exit 2 and
  the `deny` decision).
- **Argv-only, no shell — ever, and never empty.** A string-form `command` is a
  config error with a diagnostic, not something to `shlex.split` heuristically.
  This is ADR-0095 D3's posture applied to hooks: the config shape is the argv
  vector, so there is nothing for a shell to interpret and no injection surface.
  (CC supports a shell-string form and Codex executes shell text; LionAGI
  deliberately does not import that behavior — divergence Dv1-3. Import
  translation is D6's job.) `command` must additionally be a **non-empty list of
  non-empty, non-whitespace strings** — the same invalid-argv classification
  ADR-0095 applies to callback argv from every source. The rule is enforced at
  three gates: config load, `li hooks import`, and trust recording (D7). A
  violating entry **fails config load** with a diagnostic naming the file, event,
  and entry (`hooks_external: command must be a non-empty argv list`) — load
  failure rather than ADR-0095's disable-before-launch, because a hook config is
  durable declared policy, and a declared-but-undefined guard is the silent-absence
  failure mode D2 already refuses. Consequently no trust record can ever pin the
  hash of an empty argv.
- Timeout is per-hook-configurable with a 60s default (CC defaults to 600s;
  LionAGI's runtime is frequently a synchronous step inside an orchestration DAG
  where a ten-minute stall is a run-killer; 60s is generous for a guard and loud for
  a hang). The existing fixed 10s in `_make_shell_hook` remains for the legacy
  `{pre,post,on_error}` shape until that shape is migrated.
- Concurrency: hooks for one event fire sequentially in config order (a rewrite
  must see the previous rewrite's output; parallel rewriters have no defined merge).
  Across tool calls, hook concurrency mirrors the tool-call strategy: concurrent
  tool invocations run their hook chains concurrently, so a hook touching shared
  external state (a file-backed rate limiter, a counter) owns its own mutual
  exclusion — the harness serializes within a call's chain, not across calls.

### D5 — Decision semantics, enumerated

For a blocking-capable seam (`PreToolUse` via D3, `USER_PROMPT_SUBMIT` via D2):

- `permissionDecision: "allow"` (or exit 0 with no decision) — continue; if
  `updatedInput` is present at the preprocessor seam, the argument dict is replaced
  with it and the chain continues on the new value. `updatedInput` anywhere else is
  logged and ignored (there is nothing to rewrite).
- `"deny"` — raise `PermissionError(permissionDecisionReason or stderr)`; the tool
  call or prompt submission does not happen; the error travels the existing per-seam
  error path (same as today's guard-hook denial).
- exit 2 — equivalent to `"deny"` with stderr as the reason.
- `"ask"` — **fail closed**: treated as `deny` with reason
  `"hook requested interactive approval ('ask'); no interactive approval surface
  exists in this runtime — failing closed"`. LionAGI's hook execution context is
  headless (CLI runs, scheduled runs, orchestration DAG nodes); inventing a blocking
  interactive prompt inside those is a separate product decision. Fail-open
  (`ask`→`allow`) was rejected outright: a hook author who wrote `ask` expressed
  doubt, and doubt must not admit the action unattended. Deferring the interactive
  path is also forward-safe: moving `ask` from deny to a TTY prompt later is a
  strict relaxation (more permissive, and only for `ask`; headless contexts keep
  fail-closed unchanged), so no carve-out is needed now to avoid a breaking
  semantic change later.
- Any other `permissionDecision` value — **fail closed**: treated as `deny` with a
  diagnostic naming the unrecognized value. The accepted vocabulary is the
  cross-harness intersection `allow | deny | ask` (D1); Codex's schemas admit
  nothing else, and a decision channel that guesses at unknown verbs is a guard
  that can be talked past. If a harness later standardizes a new value (a `defer`,
  a `warn`), supporting it is a profile revision recorded in Notes, not a silent
  acceptance.
- `decision: "block"` on advisory events (`PostToolUse`, `UserPromptSubmit` via the
  top-level shape) — on `USER_PROMPT_SUBMIT` it blocks (that point is blocking); on
  `PostToolUse` the action already happened, so `block` cannot un-run it: the reason
  is logged and surfaced into the branch as a system-visible note, matching the
  harnesses' own "feed it back to the model" behavior as closely as the seam allows.

### D6 — Config surface and import

`.lionagi/settings.yaml` (global + project merge, existing loader) accepts a new
`hooks_external:` block in the harness shape:

```yaml
hooks_external:
  PreToolUse:
    - matcher: "bash|shell"
      hooks:
        - type: command
          command: ["uv", "run", "guards/check_cmd.py"]
          timeout: 30
  UserPromptSubmit:
    - hooks:
        - type: command
          command: ["./hooks/prompt_hygiene"]
```

- The block name is `hooks_external`, not `hooks`, because the existing `hooks:` key
  already means the `{pre,post,on_error}` tool-name shape; overloading one key with
  two schemas discriminated by structure is a parse-ambiguity trap. The legacy shape
  keeps working unchanged; both may coexist in one file.
- `type` is required and must be `command` in v1 (reserved: `http`, `mcp_tool`,
  `prompt` — see out-of-scope). Unknown `type` is a load-time error naming the value.
- **Import, not live-read, of foreign configs.** `li hooks import claude|codex
  [path]` translates a `.claude/settings.json` `hooks` block or a Codex `hooks.json`
  into `hooks_external` entries in the project `.lionagi/settings.yaml`, reporting
  per-event: imported, or rejected-with-reason (unmappable event per D2,
  untranslatable shell command per Dv1-3, empty argv per D4, unsupported handler
  type), plus the Dv1-1/Dv1-2 warnings that apply to each imported entry.
  Live-reading `.claude/settings.json` at session start was rejected: it creates an
  invisible cross-product coupling where editing Claude Code's config silently
  changes LionAGI runtime behavior, and the fail-loud rule of D2 would make LionAGI
  refuse to start on a CC config that uses CC-only events — hostile when the file
  was written for CC, informative when the user explicitly ran an import.
- **Imported entries carry provenance in the data.** Every entry `li hooks import`
  writes includes `source: imported:claude` or `source: imported:codex` as a field
  of the hook entry itself, so the trust class survives the write, file merges, and
  later commits. An entry authored in place has no `source` field and belongs to
  the tier of the file it lives in. The loader's execution rule is D7's, driven by
  this field — *where the entry sits* never upgrades *what the entry is*.
  Promotion to project-authored status is an explicit edit: deleting the `source`
  field in the project settings file, which is a reviewed, git-diffable change —
  that visible diff is the transition mechanism, and no separate promote command
  exists in v1.
- Merge semantics across global → project: entries concatenate (project entries run
  after global entries for the same event); project may not silently delete a global
  entry — removal is done where the entry is defined. This matches Codex's
  merge-not-override layering, which is the safer semantic for guards (an org-level
  guard should not vanish because a project defined its own list).

### D7 — Trust: hash-pinned approval for non-project command hooks

Command hooks are arbitrary code execution. Trust attaches to the **entry's
provenance** (the D6 `source` field plus load location), never to the file it
happens to sit in — an imported entry inside the project settings file is still an
imported entry. The tiers:

- **Project-authored** (entry with no `source` field in the repo's
  `.lionagi/settings.yaml`): trusted as code — it is versioned, reviewed, and
  diffable exactly like the code it sits next to.
- **User-authored** (entry with no `source` field in `~/.lionagi/settings.yaml`):
  trusted — the user wrote it on their own machine.
- **Imported or plugin-bundled** (entry carrying `source: imported:*` wherever it
  sits, or any hook loaded from a plugin bundle per ADR-0088): requires an explicit
  trust record before first execution. The record pins `sha256(json.dumps(argv))`
  per hook command — argv is validated non-empty first (D4), so no record ever pins
  an empty command; `li hooks trust` lists pending commands and records approval
  into `~/.lionagi/settings.yaml` (`trusted_hook_commands: [<hash>, …]`). An
  untrusted command hook does not run: blocking events fail closed (`deny` with a
  diagnostic naming the untrusted command), advisory events skip with a warning. A
  changed argv changes the hash and re-enters pending state — an update to a
  plugin's hook is a new approval, which is the point.
- **Loader rule, exact.** Before executing any external hook command the loader
  resolves: entry from a plugin bundle → tier `plugin`; else entry has `source:
  imported:*` → tier `imported`; else → the tier of the defining file (project /
  user). Tiers `project` and `user` execute; tiers `imported` and `plugin` execute
  only when the argv hash is in `trusted_hook_commands`. There is no path by which
  location, merging, or committing a file changes an entry's tier — only the
  explicit promotion edit defined in D6.
- No bypass flag in v1. Codex ships `--dangerously-bypass-hook-trust`; LionAGI's
  hook execution frequently happens in unattended scheduled runs where a bypass flag
  in a wrapper script would become permanent invisible policy. If operational
  pressure demands a bypass, it arrives as a follow-up decision with its own
  audit trail, not as a v1 convenience.
- The existing `trusted_hook_modules` gate for Python import-path hooks is unchanged
  and orthogonal (it governs in-process code loading; this governs subprocess
  execution).

## Consequences

- A hook binary written against compatibility profile v1 (the CC/Codex common
  subset plus the named divergences) runs under all three harnesses; a foreign hook
  ports with the Dv1-1…Dv1-4 checklist rather than a rewrite. Guard suites become
  write-mostly-once; the conformance matrix is what turns that from a hope into a
  tested property.
- `ActionManager.invoke` becomes the enforcement chokepoint for Branch-mediated
  tool calls with an external layer; MCP-discovered tools stop being exempt from
  tool hooks. Contributors must know that tool-hook ordering is now two-layered
  (manager-level external, spec-level internal), that the manager layer sees every
  tool invoked through it, and that direct `FunctionCalling` construction is a
  documented, tested bypass that code review keeps out of product paths.
- Two new failure modes exist and are deliberate: a hanging blocking hook denies its
  action after timeout (fail closed), and an unmappable event name refuses to load.
  Both trade convenience for the property that a configured guard is either running
  or loudly absent — never silently absent.
- The `hooks_external` name means LionAGI carries two hook-config shapes
  indefinitely. Cost accepted: the legacy shape has production users and collapsing
  the two would couple this ADR to a migration it does not need.
- Reversal cost: D1/D5/D6 are additive and could be removed by deleting the adapter
  and loader (no core surface bends around them). D3's chokepoint relocation is the
  structural commitment — reverting it would re-open the MCP coverage gap and
  reorder the chain; treat D3 as the decision to review hardest.
- `USER_PROMPT_SUBMIT` enlarges the blocking surface: a misbehaving prompt hook can
  now stall or veto every turn. Mitigated by the 60s timeout, fail-closed-on-timeout
  semantics, and the trust gate; accepted because a prompt-hygiene gate that cannot
  block is an observer, not a gate.

## Alternatives considered

- **LionAGI-native wire contract + per-harness shims** — maximum expressive freedom
  (could expose `BRANCH_CREATE` etc. natively). Lost: every existing CC/Codex hook
  needs a shim, every LionAGI hook needs two shims to travel, and the shims are
  permanent maintenance. The CC/Codex family shape's expressiveness is sufficient
  for every P1–P5 need identified, and the compatibility profile handles the
  residual divergence at far lower cost than a full dialect.
- **Route `PreToolUse` through `HookBus.TOOL_PRE`** — smallest diff, reuses the
  existing blocking point. Lost on two hard requirements: the summary-only payload
  cannot honor `updatedInput`, and MCP tools stay invisible. Keeping the audit plane
  summary-only is an ADR-0047 property worth preserving, which forces the mutation
  work onto the tool chain.
- **Extend the per-`Tool` preprocessor attributes instead of adding a manager layer**
  — preserves a single chain. Lost: MCP tools materialize as `Tool` objects at
  discovery time in a path that never passes through `AgentSpec` wiring, so per-Tool
  attributes systematically miss them; patching every discovery path is strictly more
  invasive than one chokepoint.
- **Live-read `.claude/settings.json`** — zero-step interop. Rejected for the
  coupling and fail-loud conflicts described in D6; import-with-report keeps the
  interop win and the explicitness.
- **`ask` → interactive prompt via a new approval surface** — the faithful semantic.
  Rejected for v1: it requires a UI/TTY arbitration design (headless runs, DAG
  nodes, scheduled fires) that is its own ADR-sized decision; fail-closed preserves
  safety meanwhile and is the conservative reading of `ask`.
- **Trust nothing / trust everything for command hooks** — trusting everything is
  indefensible once configs arrive via import and plugins (P5); trusting nothing
  (hash-pin even project-committed hooks) punishes the common case where the hook
  sits in the same reviewed repo as the code and adds an approval step with no
  security delta. The source-tiered rule (D7) takes each where it is defensible.

## Notes

- Naming: the existing `StopHook` exception (chain-control: "stop remaining handlers")
  is unrelated to the harnesses' `Stop` event (turn-stop arbitration). This ADR maps
  no `Stop` event, and any future ADR that does must not reuse the `StopHook` name for
  it.
- The CC and Codex hook specs are actively evolving surfaces. Compatibility profile
  v1 is pinned as of 2026-07-12: the field-guarantee table, exit-code protocol,
  decision vocabulary `allow|deny|ask`, and named divergences Dv1-1 through Dv1-4
  in D1. When either harness moves, the profile revs — a new version entry is
  appended here naming what changed and which side LionAGI takes; the loader and
  import command reference the profile version they implement. No revision happens
  silently.

## Implementation status (2026-07-13)

As of 2026-07-13, this ADR's internal-plumbing decisions are implemented and
tested; its namesake capability — running an actual external hook binary — does
not exist yet.

**Implemented and tested:**

- **D2, turn-origin verification.** The `USER_PROMPT_SUBMIT` `HookPoint` and its
  turn-origin token mechanism are fully wired: minted at the public ingresses,
  forwarded (never re-originated) through nested calls such as
  `chat_and_record()` → `chat()`, and consumed exactly once at the
  model-submission boundary. Internal turns (parse-repair, `ReAct` extension and
  final-answer steps) correctly carry the no-origin disposition and emit
  nothing. The acceptance matrix's exact-once emission counts are covered by
  dedicated integration tests, and this is the most rigorously tested area of
  the ADR.
- **D3, the `ActionManager` chokepoint.** `ActionManager.invoke()` carries a
  tool pre/post hook layer that runs for every tool routed through it — plain
  function tools, `Tool` objects, and MCP-discovered tools alike — closing the
  MCP coverage gap named in the Context. Ordering (external pre before the
  spec-level chain, tool call, spec-level post, external post),
  argument-rewrite revalidation against the tool's declared request model, and
  the invariant that `security_pre` always sees post-rewrite arguments are all
  implemented and covered by dedicated tests. The direct-`FunctionCalling`-
  construction bypass is deliberately preserved and is itself covered by a test
  asserting hooks do not fire on that path, matching this ADR's documented,
  tested-limit framing.

**Partially present (in-process analog only, no external wire mechanism):**

- The `allow`/`deny`/`ask`/unrecognized-decision semantics of D5 exist as a
  typed in-process decision object consumed by the tool-hook chain, and are
  tested there. Nothing yet parses a `hookSpecificOutput` JSON payload off a
  subprocess's stdout, so this semantics layer has no external hook to serve.
- The event-vocabulary mapping of D2 for `SessionStart`/`SessionEnd`/
  `PostToolUseFailure` targets `HookBus` points that pre-date this ADR and
  remain wired; no code parses an external event-name string out of a
  configuration file to route to them.
- The fail-closed-on-timeout principle of Dv1-4/D1 is implemented and tested
  for the in-process `ask` → deny path; no subprocess timeout code exists to
  apply the same principle to an external hook process.
- Argv-only, non-empty-list enforcement (D4) exists, but only as a plugin
  manifest validator (`HookCommand`) for ADR-0088 bundles, not as a check on
  any `.lionagi/settings.yaml` `hooks_external:` entry, because that settings
  surface does not exist (see below).
- A content-hash trust mechanism exists (`lionagi/plugins/trust.py`), but it
  pins whole declared files for plugin bundles under ADR-0088, not a
  per-hook-command argv hash gating a `hooks_external:` settings entry as D7
  specifies. The two mechanisms overlap in shape (hash-pin, revert-on-change)
  but differ in what they pin, what triggers them, and which configuration
  surface they gate.

**Not yet started:**

The capability of executing an external hook binary against LionAGI does not
exist yet. Concretely absent: the stdin wire envelope (D1) and any code that
constructs or serializes it; the dedicated exec adapter (D4) that would parse
stdout, distinguish exit 2 from other nonzero exits, and apply a configurable
per-hook timeout — the pre-existing `_make_shell_hook` executor is unmodified
and still collapses every nonzero exit to a single `PermissionError` with no
stdout read-back; the `hooks_external:` block in `.lionagi/settings.yaml` and
its loader (D6) — the settings loader reads only the legacy `hooks:` key; the
`li hooks import claude|codex` and `li hooks trust` CLI commands (D6, D7) —
neither verb exists in the CLI registry; and the cross-harness conformance
matrix (D1's own acceptance gate) — the closest existing test asserts internal
emission counts, not fixture hooks run against current Claude Code/Codex
schemas. Until this layer lands, no foreign hook suite can run under LionAGI
and no config can declare one.

One additional defect, unrelated to the above scope but found while
reconciling this ADR's D5 language against the current implementation:
`ActionManager.invoke()` awaits its post-hook run inside a `finally` block
without capturing the return value, so the `notes` list `run_tool_post_hooks`
computes (populated when a post hook returns a block reason) is discarded and
never reaches the branch as a system-visible message. This is a correctness
gap in already-merged code, independent of the unstarted work above, and
should be tracked as a bug fix on its own.
