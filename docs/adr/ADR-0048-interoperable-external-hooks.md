# ADR-0048: Interoperable external hooks (Claude Code / Codex hook contract)

- **Status**: Accepted, amended 2026-11-23 (Amendment 1)
- **Kind**: Aspirational
- **Implementation-status**: partial
- **Area**: hooks
- **Date**: 2026-07-12
- **Relations**: extends ADR-0047 (hook mechanism scopes — this ADR adds an external,
  cross-harness contract on top of the mechanisms ADR-0047 ratified; it does not move
  their ownership boundaries); builds on ADR-0095 (its D3 no-shell executable-adapter
  posture is adopted verbatim for hook commands); revisited by ADR-0120 (interception,
  observation, and durable-delivery planes) and ADR-0121 (authoritative action execution);
  Amendment 1 adds a per-leg `hooks_from` input to `agent.submit` and leaves hook
  rendering for CLI legs to ADR-0134

## Amendment note (2026-08-16)

The Context and problem statements below describe the 2026-07-12 baseline. They are historical
motivation, not current-state claims: `lionagi/hooks/external.py`, configuration import/trust, and
conformance tests now exist as recorded in the implementation-status section. Residual design is
governed by ADR-0120/ADR-0121; no reader should implement the old “only external path” premise.

## Amendment 1 (2026-11-23): a leg takes its parent's deny rules and hooks from a file named at submit

**Status**: Accepted (2026-11-23).

Issue #4546 asks for "`hooks.import` over MCP, or `agent.submit(hooks_from=\"<config path>\")`,
snapshotted at submit like `mcp_config`", because "A deployment that delegates only through the MCP
server cannot give a leg its parent's deny rules without a shell step outside the server." A
caller-named `mcp_config` is not snapshotted today: `agent.submit` passes the named path through to
the child's `--mcp-config` (`lionhub/src/lionhub/serve/mcp/jobs.py::submit`, its branch for a
caller-named config), and only a config discovered from the launch directory is copied into the run
directory (the same function, through `_write_mcp_server_snapshot`). This amendment gives
`hooks_from` the snapshot the request asks for, with `prompt_file` as the precedent
(`lionhub/src/lionhub/serve/mcp/dispatch.py::_resolve_prompt`: an absolute path, read once at
submit, capped, empty refused). It does not change `mcp_config`.

It adds to D6 and D7 and replaces nothing in them: `li hooks import`, the `hooks_external` block, the
D7 tiers and the loader rule stay as written.

### H1. Surface

`agent.submit` gains three inputs. Two name the file: `hooks_from`, the path of a hooks
configuration, and `hooks_from_format`, `claude` for a Claude Code `settings.json` shape or `codex`
for a Codex `hooks.json` shape, required whenever `hooks_from` is given. They mirror the source
argument of `li hooks import`. `li agent` gains no flag a person types: the submit hands the child its snapshot
through a flag hidden from help, as it hands `--mcp-submit-snapshot` today.

The third, `hooks_from_carry`, is a list drawn from `hooks` and `deny`, and defaults to both. `deny`
covers the file's `permissions.ask` rules too, which H3 carries as deny rules. A kind the caller
leaves out is not carried, and the submit result says it was left out at the caller's request. A
kind that is carried and cannot be applied still refuses the submit (H3, H5). Nothing is dropped
unless the caller names it, so D2's fail-loud rule holds, and the caller gains no access it lacked:
it could already submit the leg without `hooks_from`.

`hooks.import` and `hooks trust` stay unreachable from the MCP server
(`lionhub/src/lionhub/serve/mcp/verbs.py::FENCED_PATHS` and the absent `hooks import` entry in the
same file). Serving `hooks.import` would write the project's `.lionagi/settings.yaml`, a lasting
change that reaches the parent and every later run, and its entries still could not run without the
fenced trust step. `hooks_from` changes one leg only.

Schedules do not take `hooks_from` in this change. A schedule fire does not call `agent.submit`: it
starts `li agent` with only the agent, project, model and prompt
(`lionhub/src/lionhub/orchestration/scheduler/subprocess.py::build_argv`), and
`action_extra_args` refuses any token that begins with `-`
(`lionhub/src/lionhub/orchestration/schedule/action_validation.py::_validate_extra_args`), so no
schedule can carry the input. A schedule field for it, and the point at which a fire would read it, is a later decision.

### H2. Reading the file

The file is read once, at submit, under these rules. Every refusal names the rule it broke and the
path as given, and carries no bytes from the file: no parser excerpt and no offending value.

1. The path is absolute. `-` and relative paths are refused.
2. The server opens it once with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC` and calls `fstat`
   on that descriptor. A symbolic link as the last component is refused by the open. Links in parent
   directories are resolved, and the resolved path is recorded. `O_NONBLOCK` keeps a FIFO or a
   device at the path from holding the server before rule 3 refuses it.
3. The descriptor must be a regular file.
4. At most 256 KiB is accepted. The server reads at most the cap plus one byte from the descriptor,
   and a file that yields more than the cap is refused, never truncated. The size `fstat` reported
   is not used for this.
5. The bytes are UTF-8 JSON whose top level is an object. `NaN`, `Infinity` and `-Infinity` are
   refused, as `mcp_config` refuses them
   (`lionhub/src/lionhub/orchestration/mcp_config.py::_read_config`).
   A name repeated inside any object is refused: a parser that kept the last one would let a later
   entry replace a deny rule with no trace.
6. The accepted bytes are written into the run directory as a new file, created exclusively with
   owner-only mode. The run record carries the copy's sha256, the path as given and the resolved
   path. The leg reads only the copy. The leg and every resume hash the copy and refuse to start on a
   mismatch with the record. An edit to the original after submit reaches no leg.
7. There is no containment root: the path is the caller's, and the server can already read it. H3's
   report, which names only known keys, keeps the result from listing the keys of an arbitrary JSON
   file.

### H3. What is carried, what is reported, what refuses

Three keys are guards: `hooks`, `permissions.deny` and `permissions.ask` (the last two in a Claude
Code file only; a Codex `hooks.json` has no permission rules). Every other key is not a guard.

- A key that is not a guard is not carried, and the submit result reports it. The result names a key
  only when it is in a fixed list of Claude Code settings keys kept beside the parser (among them
  `model`, `env`, `apiKeyHelper`, `sandbox`, `disableAllHooks`, `permissions.allow`,
  `permissions.additionalDirectories` and `permissions.defaultMode`); any other key is counted, not
  named. Nothing that widens a leg's access is carried.
- An ask rule is carried as a deny rule. D5 already settles what `ask` means where no one can
  answer: it fails closed and is treated as `deny`. Each ask rule is rendered through H6 like a deny
  rule, and the submit result reports the count: "N ask rules rendered as deny: a leg has no one to
  ask".
- A guard that cannot be carried refuses the submit. That is a hook entry the D6 translator would
  reject (an event with no seam under D2, a command that cannot become an argv vector under Dv1-3,
  an empty argv under D4, a handler type other than `command`); a hook entry, deny rule or ask rule
  for a provider that cannot apply it (H5); and a deny or ask rule that H6 refuses. The refusal
  names the entry by event or list and its index and gives the reason; it quotes no content. A leg
  that starts without a guard its caller believes attached is the failure D2 exists to stop.
- A guard kind the caller leaves out through `hooks_from_carry` (H1) is neither carried nor refused,
  and the result names it as left out at the caller's request.

`li hooks import` keeps its own behaviour, reporting a rejected entry and importing the rest; the
refusal above applies to `hooks_from` only.

### H4. Trust

A deny rule only narrows what a leg may do, so it needs no trust record. A hook command runs code
with the account's authority, so every hook entry carried from a `hooks_from` file is tagged
`source: imported:<format>` and falls in D7's `imported` tier: it runs only when a
`trusted_hook_commands` record matches its argv hash, resolved executable path and content digest,
as the loader checks today (`lionhub/src/lionhub/hooks/external.py::_trust_status`).

- The submit checks every hook command against the records and refuses on any command without a
  match, naming `li hooks trust` as the step and quoting nothing from the file. Execution checks
  again through the private-copy binding that D7's implementation already uses.
- `li hooks trust` gains `--from <path> --format claude|codex`. It reads the named file under the
  H2 rules and through the same translator, lists the commands that have no matching record, and
  records approval on confirmation, so a person can trust a file that was never imported. It is a
  CLI step only: `hooks trust` stays in the MCP server's fenced set, and the MCP path never records
  trust. An agent can attach only commands a person has already approved.
- A hook command is refused at submit when its resolved executable, or any argument that names an
  existing file, lies inside a directory the leg can write: its working directory, the child
  sandbox's `allow_write` roots and the run's scratch directory. The digest check at execution does
  not stop a leg that rewrites its own guard during the run, and an interpreter argument
  (`uv run guards/check.py`) would otherwise put the guard's code outside what the record pins.
  A relative argument is resolved, like a relative executable
  (`lionhub/src/lionhub/hooks/external.py::resolve_hook_executable`), against the directory the
  hook runs in: for a leg, its working directory `AgentSpec.cwd`, which
  `lionhub/src/lionhub/agent/factory.py::_wire_external_hooks` passes to each hook (the process's
  own directory when it is unset). So a relative argument naming an existing file in that tree is
  always refused. The check narrows what a leg can rewrite under its own guard; it does not prove
  the guard's code pinned: a guard that imports a module from a writable directory, or reads one at
  run time, is not caught by it.

### H5. Per provider

- **API-model legs.** Hook entries go onto `AgentSpec.external_hooks` and are wired by the existing
  seam (`lionhub/src/lionhub/agent/factory.py::_wire_external_hooks`). Deny and ask rules refuse
  the submit: lionagi's tool loop has no executor for Claude Code permission-rule syntax, and this
  amendment does not invent a mapping.
- **claude_code legs.** Deny and ask rules are carried; hook entries refuse the submit. The rules join
  the leg's settings through the additive merge `claude_settings_argument` uses
  (`lionhub/src/lionhub/agent/child_sandbox.py::_merge` and `claude_settings_argument`: lists gain
  entries, a `sandbox` block and `disableAllHooks` are refused). With the child sandbox on, that
  merge produces the one `--settings` value; with it off, the merged value is the leg's own
  `settings` and is rendered as the one `--settings` flag
  (`lionhub/src/lionhub/providers/anthropic/claude_code.py::ClaudeCodeRequest.as_cmd_args`). Hook
  entries reach CLI legs when ADR-0134 is decided: that record designs the Claude hook render as one
  registry compiled at the spawn seam, and a second, per-leg render written ahead of it would fork
  it. Issue #4546 stays open for that part.
- **codex, gemini and every other CLI provider.** A snapshot that carries any hook entry, deny
  rule or ask rule refuses the submit until ADR-0134 gives the provider a renderer. Nothing is dropped silently.

The first two bullets have a consequence. Most Claude Code settings files hold both hooks and deny
rules, and until ADR-0134 such a file, carried whole, is accepted by no provider: an API-model leg
refuses its deny rules and a claude_code leg refuses its hooks. `hooks_from_carry` (H1) is the way
through. A caller that names only `deny` starts a claude_code leg with the file's deny and ask rules,
and one that names only `hooks` starts an API-model leg with its hooks; each result names the kind
left out.

### H6. Rule paths

A rule written for the parent must name, in the leg, the files it named for the parent. Claude Code's
documentation (https://code.claude.com/docs/en/permissions, the sections from "Read and Edit" through
"Special characters and ! negation", read 2026-11-23) gives four spellings for `Read` and `Edit`
rules: `//path` from the filesystem root, `~/path` from the home directory, `/path` relative to the
settings source, and `path` or `./path` relative to the current directory. A `/path` rule anchors at
the primary working directory in project and local settings, at `~/.claude` in user settings, and at
the file's own directory for a file passed with `--settings`. On depth it says: "Bare filenames follow
gitignore semantics and match at any depth, so `Read(.env)` and `Read(**/.env)` are equivalent";
"Deny and ask rules: `Read(secrets/**)` matches a directory named `secrets` at any depth under the
current directory"; and "`Edit(/src/**)` and `Edit(src/components/**)` match only at their anchored
location". A `!` pattern is a gitignore negation read relative to the current directory.

The submit renders each `Read` or `Edit` deny or ask rule as follows. `<dir>` stands for each of two
directories, the source project directory and the leg's working directory, so a rule rendered
"twice" appears once for each; `x` is the pattern with any leading `./` removed.

| Spelling in the source file | Rendered for the leg |
| --- | --- |
| `//x`, `~/x` | as written |
| `/x` in a project or local settings file | twice: `//<dir>/x` |
| `/x` in a user settings file | `~/.claude/x` |
| a relative pattern with two or more directory segments before its last part and no leading `./` (`src/components/**`) | twice: `//<dir>/x` |
| every other relative pattern: a bare name (`.env`, `*.env`), one directory segment (`secrets/**`, `src/**`), and any `./` pattern | twice: `//<dir>/**/x` |
| any `!` pattern | refused |

A `/x` rule is never left as written: in the settings value lionagi hands the child it would anchor
somewhere else. The source file is a project or local settings file when it is
`.claude/settings.json` or `.claude/settings.local.json` under a directory, which is then the source
project directory, and a user settings file when it is `~/.claude/settings.json`. A `/x` rule in any
other file refuses, and so does a relative pattern in a file that is not a project or local settings
file: the directory it was written against is not known at submit. A relative rule names the parent
tree's file and, in a leg's worktree, the leg's own copy. Both are files the rule's author meant to
deny, and a deny only narrows, so both are rendered. Where the documentation gives a shape any-depth
matching, or the shape is in doubt, the rule takes the any-depth form: a rule rendered deeper than
the parent applied it denies more, never less, while the anchored form of `Read(secrets/**)` would
let the leg read `vendor/secrets/x`, which the parent could not.

Rules for other tools:

- A rule carries as written only when it has no path anchor to lose: a bare tool name, a `*` form,
  and rules for `Bash`, `PowerShell`, `WebFetch`, MCP tools and `Agent`.
- A `Cd` rule carries when it is written `//x` or `~/x`. The documentation says "Path patterns share
  the `//`, `~/`, and `/` anchors from Read and Edit rules", so a `/x` or relative `Cd` rule would
  anchor somewhere else in the leg. It refuses, naming the `//` spelling.
- A rule for `Write`, `Glob`, `Grep`, `NotebookEdit` or `MultiEdit` with a path other than `*`
  refuses. Claude Code "accepts a rule with a path other than `*` for one of the tools below but
  doesn't match a read, search, or edit against it", so such a rule guards nothing in the parent
  either, and whoever wrote it believes a guard is attached. The refusal names the `Edit` spelling
  for `Write`, `NotebookEdit` and `MultiEdit`, and the `Read` spelling for `Glob` and `Grep`.
- A rule with a specifier for any other tool refuses, naming the tool.

The tests show that the rendered rule text reaches the child's settings. The implementation merges
with a probe record, made with the probe harness of #4750, that shows the child refuse a scripted
read or command under each rendered shape, beside a control with no rule: `//<dir>/x`,
`//<dir>/**/<name>`, `~/x`, one ask rule rendered as deny, and one `Bash` rule. A shape the record
does not show refused is refused at submit.

### H7. Tests the implementation carries

- A deny rule reaches a claude_code child's `--settings`, with the child sandbox on and with it off.
- An ask rule reaches the child's `--settings` as a deny rule and the result reports the count; an
  ask rule that H6 refuses refuses the submit.
- A relative deny rule, with a leg working directory different from the source file's directory,
  renders both absolute forms.
- A bare name (`.env`) renders `//<dir>/**/.env` for both directories.
- `secrets/**` and `./secrets/**` render the any-depth form for both directories, and
  `src/components/**` the anchored form. A `!` pattern refuses, and so does a relative pattern in a
  file that is not a project or local settings file.
- `Cd(//x)` and `Cd(~/x)` carry as written, and `Cd(/x)` and `Cd(x)` refuse. `Write(src/x)` refuses
  naming the `Edit` spelling, and `Grep(src/x)` naming the `Read` spelling. A `Bash` rule, a
  `WebFetch` rule and a bare tool name carry as written.
- A file holding both hooks and deny rules, submitted with `hooks_from_carry` naming only `deny`,
  starts a claude_code leg with the deny rules in its settings, and the result names `hooks` as left
  out at the caller's request. The same file with the default refuses.
- A rejected hook entry refuses the submit, naming event and index and no content.
- An untrusted hook command refuses, and the same file with that hook removed starts.
- A hook program inside the leg's working directory refuses, and so does a relative argument naming
  an existing file there.
- An API-model leg with deny rules refuses; a claude_code leg with a hook entry refuses; a codex leg
  with any entry refuses.
- An unknown top-level key is counted and not named; `permissions.allow`, `env` and `sandbox` are
  absent from the child's settings and named in the result.
- Each H2 rule refuses on its own fixture (a relative path, a link as the last component, a FIFO, a
  file one byte over the cap, `NaN`, a repeated name), and no refusal carries bytes from the file.
- A copy changed between submit and start refuses, and an edit to the original after submit does not
  reach the leg.
- `li hooks trust --from` records trust for a file that was never imported, and `hooks trust` stays
  unreachable from the MCP server.

## Context

LionAGI has three in-process hook mechanisms with distinct scopes, ratified by
ADR-0047: the session-scoped `HookBus` (`lionagi/hooks/`, observe/audit plane with one
hard-wired blocking point at `TOOL_PRE`), the tool-scoped preprocessor/postprocessor
chain (`lionhub/src/lionhub/agent/spec.py::HooksMixin`, full-payload mutation), and the
service-scoped `HookRegistry` on `iModel` events. All three register in-process Python
callables. The only way to run an *external program* as a hook today is
`lionhub/src/lionhub/agent/settings.py`'s `_make_shell_hook`: argv-only subprocess, stdin JSON,
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

## Implementation status (amended 2026-08-16)

The 2026-07-13 status previously recorded here was wholly stale. The namesake
external-hook capability now exists and is tested.

**Implemented and tested:**

- **D1/D4/D5, wire execution.** `lionhub/src/lionhub/hooks/external.py` constructs the
  external envelope, executes a non-empty argv command without a shell, applies
  the per-hook timeout, parses stdout decisions, distinguishes exit-code
  behavior, validates rewrites, and preserves blocking versus advisory seams.
- **D2, event mapping and turn origin.** `USER_PROMPT_SUBMIT` is wired exactly
  once for a user-originated turn and is blocking, as is `PreToolUse`; internal
  turns do not mint a user origin. The mapped session/tool events have
  conformance coverage.
- **D3, manager path.** `ActionManager.invoke()` runs the external pre/post
  sequence for ordinary and MCP-discovered Tools, revalidates rewritten input,
  and re-runs security processing against that input. Direct
  `FunctionCalling` construction remains a tested bypass and is no longer an
  acceptable end-state under ADR-0121.
- **D6/D7, configuration and trust.** `agent/settings.py` parses
  `hooks_external:`. `li hooks import claude|codex` translates foreign configs,
  and `li hooks trust` records content-pinned approval. Execution binds a
  private copy to the resolved executable/trust record to avoid re-resolution
  after approval.
- **Conformance.** Fixture tests exercise external execution, decision mapping,
  imports, trust, timeout, rewrite, and both blocking/advisory behavior.

**Residual work and corrected boundaries:**

- The legacy `_make_shell_hook` path still exists beside the external executor
  and uses a different no-stdout contract. It must become a compatibility
  adapter or be retired; it is not a second external-hook authority.
- Some external execution and legacy paths use direct low-level concurrency or
  JSON handling rather than LionAGI's internal concurrency/serialization
  primitives. ADR-0119/ADR-0120 govern that consolidation.
- Studio/frontend configuration still drifts from the runtime profile: event
  vocabulary and command representation are duplicated and partly
  Claude-specific. The versioned runtime declaration must generate or
  contract-test those projections.
- `StopHook` (stop sibling handlers) remains unrelated to a provider `Stop`
  event (turn-stop arbitration). No adapter may conflate them.
- External hooks attached to the manager path do not close direct `FunctionCalling` or other
  callable routes. Fresh plugin Tools are resolved before manager-owned external pre/post hooks and
  therefore do receive those hooks; their separate spec-level PermissionPolicy attachment gap is
  tracked by ADR-0044/ADR-0121. ADR-0121 makes ActionExecutor the authoritative boundary.
- Post-hook reason notes remain metadata unless an owning operation explicitly
  projects them into a message. Observation delivery is governed by ADR-0120;
  it is not silently upgraded into operation control.
