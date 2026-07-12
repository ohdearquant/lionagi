# ADR-0048: Interoperable external hooks (Claude Code / Codex hook contract)

- **Status**: Proposed
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

Meanwhile the two dominant agent harnesses have converged on one external-hook wire
contract. Claude Code and Codex CLI both ship: a JSON envelope on stdin carrying
`session_id`, `cwd`, `hook_event_name`, and per-event fields (`tool_name`,
`tool_input`, `tool_response`); an exit-code protocol (0 = success and stdout is
parsed as JSON, 2 = block with stderr as the reason, other = non-blocking failure);
and a stdout decision shape (`hookSpecificOutput.permissionDecision` ∈
`allow|deny|ask` with `permissionDecisionReason` and optional `updatedInput` for
`PreToolUse`; top-level `decision: "block"` + `reason` for most other events). Both
use the same nested config shape (`hooks.<EventName>` → `[{matcher, hooks: [{type,
command, timeout}]}]`) and largely the same event names. Codex additionally ships a
hash-pinned trust gate: a non-managed hook's exact command must be explicitly trusted
before it runs.

This convergence is an opportunity with a deadline attached. Users who already
maintain a hardened hook suite for Claude Code or Codex (guards, formatters, audit
loggers, notification bridges) should be able to point LionAGI at it and have it
work; hooks written for LionAGI should run unmodified under either harness. If
LionAGI invents a third wire contract, every hook gets written twice and the
ecosystem's existing hook tooling is unusable here.

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
| External wire contract | D1: adopt the converged CC/Codex stdin/exit-code/stdout-JSON contract |
| Event vocabulary and mapping | D2: fixed mapping table; `USER_PROMPT_SUBMIT` added with its emit site; unmapped events fail loud at load |
| Which internal seam serves tool events | D3: tool pre/post events route to the tool-hook chain, relocated to the `ActionManager` invoke chokepoint so MCP tools are covered |
| Executor | D4: one exec adapter extending `_make_shell_hook` — argv-only, stdout parse-back, per-hook timeout |
| Decision semantics | D5: `allow`/`defer` continue, `deny` raises, `ask` fails closed; `updatedInput` honored only at the preprocessor seam |
| Config surface | D6: CC-shaped `hooks:` block in `.lionagi/settings.yaml`, plus explicit import of CC/Codex hook configs |
| Trust | D7: hash-pinned trust for command hooks that are not project-committed |

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

### D1 — Adopt the converged external-hook wire contract

LionAGI's contract for external hook processes is the CC/Codex contract, not a new
one.

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

Common fields (`session_id`, `cwd`, `hook_event_name`) are always present. Per-event
fields follow the CC/Codex field names exactly (`tool_name`, `tool_input`,
`tool_response`, `prompt`). The translation is mechanical and total: LionAGI's tool
argument dict (the `arguments` mapping a `FunctionCalling` invocation carries) is
placed under `tool_input` verbatim — it is already an arbitrary JSON-serializable
dict, so no reshaping occurs; a returned `updatedInput` replaces that same dict
whole (no per-key merge — partial rewrites are the hook's job to construct from the
`tool_input` it was sent). `tool_name` is the registered tool name string
(`Tool.function`), and `tool_response` is the tool's result as JSON where
serializable, else its string form. One LionAGI addition: `harness: "lionagi"` — a
hook that must behave differently per harness keys off this; CC and Codex omit it,
so its absence means "not lionagi." Fields LionAGI cannot populate (for example
`transcript_path` when no transcript file exists for the surface) are omitted, never
sent as fabricated values.

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
`decision: "block"` + `reason` for other events; unknown fields ignored (forward
compatibility with harness spec evolution).

Why this way: the alternative — a LionAGI-native contract with an adapter shim per
harness — was rejected because the two harnesses have *already* converged on one
contract between themselves; a third dialect creates permanent translation liability
for zero expressive gain. Adopting the contract verbatim means the same guard binary
serves three harnesses, and the existing ecosystem of published CC/Codex hooks runs
on LionAGI unmodified. The contract is external-facing and versioned by the harness
docs; where CC and Codex diverge in the future, LionAGI follows the intersection and
documents the divergence in this ADR's Notes.

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
  as its emit sites** — plural, because the API path and the CLI/stream path are
  architecturally disjoint and share no single "instruction submission" function:
  one `blocking_emit` in the `communicate` middle before its chat call, and one in
  the `run` middle before streaming begins. Payload: `{session_id, branch_id,
  prompt}` where `prompt` is the rendered instruction text. Adding the enum member
  without both emit sites is forbidden; ADR-0047 already documents four never-wired
  hook points as exactly this trap, and this ADR does not add a fifth.
- **Fires exactly once per user-originated turn.** `operate()` delegates to
  `communicate` through the `Middle` protocol, so a naive emit at both layers would
  double-fire on one turn; a turn-scoped emitted-flag on the operation context
  guards this — the outermost entry emits, the delegated inner call sees the flag
  and stays silent. The point fires **only for a user-originated instruction**:
  internal instructions the runtime synthesizes (ReAct sub-steps, parse-retry
  turns) never emit it. The discriminator is placement — the emits live in the
  top-level middle entries named above, never in `a_add_message` or any shared
  message-construction primitive, because internal turns traverse those shared
  primitives too and a prompt guard firing on the model's own reasoning steps
  would block the run from inside. Implementation acceptance: an integration test
  asserting exactly one emission for an `operate→communicate` turn and zero
  emissions for a ReAct internal step.
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
chain, not `HookBus` — and that chain moves to the one place every tool call passes
through: `ActionManager.invoke`.

The contract:

- `ActionManager` gains an optional pre/post processor pair applied inside `invoke`,
  around the `Tool` call, for **every** tool — plain function tools, `Tool` objects,
  and MCP-discovered tools alike. The existing per-`Tool` `preprocessor`/
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
- **Argv-only, no shell — ever.** A string-form `command` is a config error with a
  diagnostic, not something to `shlex.split` heuristically. This is ADR-0095 D3's
  posture applied to hooks: the config shape is the argv vector, so there is nothing
  for a shell to interpret and no injection surface. (CC supports a shell-string
  form; LionAGI deliberately does not import that part of the contract — a portable
  hook config that must also run on LionAGI uses the argv form, which both harnesses
  accept.)
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
- `"defer"` — continue (defer means "let the normal permission flow decide"; LionAGI's
  normal flow at that point is the remaining chain, so deferral is continuation).
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
  per-event: imported, or rejected-with-reason (unmappable event per D2, shell-string
  command per D4, unsupported handler type). Live-reading `.claude/settings.json` at
  session start was rejected: it creates an invisible cross-product coupling where
  editing Claude Code's config silently changes LionAGI runtime behavior, and the
  fail-loud rule of D2 would make LionAGI refuse to start on a CC config that uses
  CC-only events — hostile when the file was written for CC, informative when the
  user explicitly ran an import.
- Merge semantics across global → project: entries concatenate (project entries run
  after global entries for the same event); project may not silently delete a global
  entry — removal is done where the entry is defined. This matches Codex's
  merge-not-override layering, which is the safer semantic for guards (an org-level
  guard should not vanish because a project defined its own list).

### D7 — Trust: hash-pinned approval for non-project command hooks

Command hooks are arbitrary code execution. The trust rule, by config source:

- **Project-committed** (`.lionagi/settings.yaml` inside the repo): trusted as code —
  it is versioned, reviewed, and diffable exactly like the code it sits next to.
- **User-global** (`~/.lionagi/settings.yaml`): trusted — the user wrote it on their
  own machine.
- **Imported or plugin-bundled** (output of `li hooks import`, or a plugin's hook
  block per ADR-0088): requires an explicit trust record before first execution. The
  record pins `sha256(json.dumps(argv))` per hook command; `li hooks trust` lists
  pending commands and records approval into `~/.lionagi/settings.yaml`
  (`trusted_hook_commands: [<hash>, …]`). An untrusted command hook does not run:
  blocking events fail closed (`deny` with a diagnostic naming the untrusted
  command), advisory events skip with a warning. A changed argv changes the hash and
  re-enters pending state — an update to a plugin's hook is a new approval, which is
  the point.
- No bypass flag in v1. Codex ships `--dangerously-bypass-hook-trust`; LionAGI's
  hook execution frequently happens in unattended scheduled runs where a bypass flag
  in a wrapper script would become permanent invisible policy. If operational
  pressure demands a bypass, it arrives as a follow-up decision with its own
  audit trail, not as a v1 convenience.
- The existing `trusted_hook_modules` gate for Python import-path hooks is unchanged
  and orthogonal (it governs in-process code loading; this governs subprocess
  execution).

## Consequences

- A hook binary written against the CC/Codex contract runs on LionAGI unmodified
  (argv form), and hooks written for LionAGI run under CC and Codex. Guard suites
  become write-once.
- `ActionManager.invoke` becomes the single tool-call chokepoint with an
  external-enforcement layer; MCP-discovered tools stop being exempt from tool
  hooks. Contributors must know that tool-hook ordering is now two-layered
  (manager-level external, spec-level internal) and that the manager layer sees
  every tool.
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
  permanent maintenance. The converged contract's expressiveness is sufficient for
  every P1–P5 need identified.
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
- The CC and Codex hook specs are actively evolving surfaces. This ADR pins the
  intersection contract as of 2026-07-12 (envelope fields, exit codes, decision
  shapes listed in D1); divergences discovered later are recorded here with the
  chosen side.
