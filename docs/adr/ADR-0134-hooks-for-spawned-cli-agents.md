# ADR-0134: Hooks for spawned CLI agents: one registry, rendered at the spawn seam

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: hooks
- **Date**: 2026-10-19
- **Relations**: extends ADR-0120 D6 (one versioned external hook profile) and ADR-0048 (external
  hooks: D4 argv, D7 trust tiers); builds on ADR-0030 D4 (Proposed; one subprocess entry point for
  agentic providers); depends on #3671 (open: Codex user-config isolation and the project-layer
  walk); read with ADR-0133 (proposed in #3657, open: admission at the same entry point) and
  ADR-0094 (automated PR review pipeline)

lionagi starts `claude` and `codex` processes for every CLI agent it runs. This record makes lionagi
the only place the hooks those processes run are declared, and makes it impossible for a `claude` or
`codex` spawn to start without them. Other engines lionagi spawns have no renderer; a required entry
that names them refuses their spawn (D8).

The first hook it carries serves one redaction requirement: secret values are replaced by tokens
from a single redaction table before they reach the model's context and before the engine writes
its transcript. D7 meets it on the two paths lionagi can see. lionagi redacts the prompt text it
hands the engine before the spawn. A tool result is redacted at the pre-tool hook, which either
routes the call's result through the redactor or denies the call, because neither engine is known
to call a hook before it persists a tool result. Post-tool, stop and exit scans check and scrub what
substitution missed. A value that reaches the engine by neither path, such as an instruction file
the engine loads on its own, is found by the stop and exit scans of the transcript, not prevented.

## Context

A CLI agent is a separate program with its own hook system. Claude Code reads hook definitions from
its settings sources; Codex reads them from its config layers and `hooks.json` files. lionagi's own
hook machinery (ADR-0047 scopes, ADR-0048 external hooks, ADR-0120 planes) runs inside lionagi's
process, around lionagi's own tool loop. None of it reaches into a CLI agent's process: when lionagi
spawns `claude -p` or `codex exec`, the hooks that run are whatever that program finds on the
machine.

**P1 — the hooks a spawned agent runs are decided by the machine, not by lionagi.** On `main` only
the Studio Operator sets `setting_sources`; the change in #3671 adds it for flow and fan-out workers
(built by `build_worker_branch`). A Claude agent started by `li agent`, a flow's orchestrator, a
fan-out's synthesis step or any other spawn path runs with the CLI's default sources (user, project,
local): the user's interactive `~/.claude/settings.json` hooks, plugins and permissions, and the
hooks of whatever repository the agent's working directory is in. A Codex agent loads the user's
`config.toml` and `hooks.json` unless its request sets `CodexCodeRequest.ignore_user_config`, which
the change in #3671 adds and sets for flow and fan-out workers and for a run handed an empty MCP set; every
Codex agent still loads the project layers between its working directory and the project root that
a trust source marks trusted. The change in #3671 refuses a flagged spawn whose project layer
declares `mcp_servers`; it does not look at hooks a project layer declares. Two consequences,
opposite in sign: an interactive hook the user wrote for themselves runs inside every automated
agent, and a cloned repository's own hook files execute commands in an agent lionagi started. Once
spawned agents are isolated from the user layer (which they must be, for P1's first half), a Claude
agent runs with no hooks at all and a Codex agent runs only the hooks of a trusted project layer
until D5's refusal lands; either way a guard the operator relies on disappears without a message.

**P2 — the declaration surfaces that exist do not reach spawned agents.**

- The hook library (`lionagi/studio/services/hooks_library.py`, `LIONAGI_HOME/hooks/library.json`)
  defines hooks once by name, and consumers attach them to events. Its only renderer,
  `materialize_claude_hooks`, has one caller: the Operator.
- Agent profiles: Studio's profile writer accepts and validates a `hooks` assembly
  (`lionagi/studio/services/agents.py`), and `li agent` reads it only to warn that CLI-spawned runs
  do not apply it (`lionagi/cli/agent.py`, "A saved guard the leg silently does not run is worse
  than no guard").
- `hooks_external:` in `.lionagi/settings.yaml` (ADR-0048 D6, with `li hooks import`) serves
  lionagi's in-process seams for API-model agents. It never reaches a CLI process.

**P3 — many paths start an agent; one function spawns it.** The paths: `li agent` (new and
resumed), `li o flow` and `li o fanout` workers and the fan-out synthesis agent, `li play` (rewritten
to `li o flow`), the MCP server's `agent.submit` / `flow.submit` / `fanout.submit` / `play.submit`
(each starts a `li` process, `lionagi/mcp/jobs.py`), schedules whose action is an agent or flow
(each starts a `li` process, `lionagi/studio/scheduler/engine.py`), engines that build agents
through `create_agent` (`lionagi/agent/factory.py`), and the Studio Operator. One more spawn hides
inside the Claude provider: the `auto_finish` continuation, which deep-copies the request, sets
`--resume` to the first turn's session and calls the stream function again. It is the only resume
the provider starts on its own; every other resume is a caller's request, such as a resumed
`li agent` run. A render placed in any model builder misses the continuation and the persisted
request of a resumed run. Every one of these ends in the provider's `_ndjson_from_cli` and then in
`ndjson_from_cli()` (`lionagi/providers/_cli_subprocess.py`), the single subprocess entry point that
ADR-0030 D4 (Proposed) names and that ADR-0133 (#3657) proposes to use for admission.

**P4 — the one working render fails open and shares one file.** The Operator's
`materialize_settings_file` returns `None` on a broken config or library, and its engine call site
catches every exception and runs the turn without hooks (`lionagi/studio/services/operator_hooks.py`,
`lionagi/studio/operator/engine.py`). `materialize_claude_hooks` skips an attachment whose hook was
deleted from the library. The rendered file is one path, `LIONAGI_HOME/operator_hooks.settings.json`,
rewritten on every turn, so two concurrent turns race on it. For a guard, each of these is a silent
removal.

**P5 — the settings path rule keeps a render out of most working directories.** The Claude request
validates `settings` as a repository-relative path with no traversal and contains it inside the
agent's repository (`lionagi/libs/path_safety.py`, `ClaudeCodeRequest`). That guard is right for a
caller-supplied path. It means a file rendered under `LIONAGI_HOME` reaches an agent only when the
agent's working directory is an ancestor of it, which the Operator's own comment records as the
reason a custom execution root runs hook-less.

**P6 — Codex runs a command hook only after its trust step.** A render lionagi writes has to be
trusted by Codex. The documented ways are trust records keyed by a hash of each hook source, or a
flag that bypasses hook trust for the whole invocation. The second also trusts every other enabled
hook in that invocation, including any a repository's project layer declares.

**P7 — some guarantees are not an event.** The first hook this registry carries is a redaction
hook: a secret value the agent reads must not appear in the engine's own rollout or transcript. A
`Stop` hook runs after the transcript already holds the bytes. A post-tool hook that blocks a
result with a reason changes what the model sees next, which is not the same as what the engine
wrote to disk. An assembly that attaches the hook and reports it "installed" proves nothing about
persistence. The registry needs a way to say what an entry requires of its target, and a spawn
needs to refuse a target that cannot provide it.

| Concern | Decision |
|---------|----------|
| Where a hook for a spawned agent is declared | D1: one registry, `LIONAGI_HOME/hooks/agents.json`, over the existing library; later layers add, never remove |
| Event vocabulary and what an entry may require | D2: neutral events plus engine-labeled events per ADR-0120 D6; each target publishes a capability table proven by conformance; entries declare `requires` |
| Where the render happens | D3: one compiler, required by each CLI provider's spawn; `ndjson_from_cli()` refuses an agentic spawn that carries no compiled render |
| Claude render | D4: a per-run settings file under `LIONAGI_HOME`, emitted by the spawn, with setting sources empty |
| Codex render | D5: an inline config layer, user config ignored, hook-declaring project layers refused, trust by computed records |
| Failure policy | D6: every failure before the spawn refuses it with a typed refusal; no hook-less fallback |
| What the redaction entry guarantees and how | D7: write-time protection is lionagi's own prompt redaction plus pre-tool substitution or denial; post-tool, turn-end and a lionagi-fired exit event verify and scrub |
| Scope | D8: every Claude and Codex spawn through `ndjson_from_cli()`; other engines refuse required entries they cannot render |

Out of scope:

- lionagi's in-process hooks for API-model agents: ADR-0048 and ADR-0120 own them. Rendering this
  registry into those seams is DEFERRED (D8).
- Programs a schedule's `command` action starts directly: they are outside lionagi. They are covered
  exactly when they start their agents through lionagi (`li agent`), and not otherwise.
- Engine-managed policy (an organization's managed settings): lionagi cannot disable it. Acceptance
  A4 records what it injects; this ADR makes no claim over it.
- What the redaction hook does inside its own process: the hook is a separate program. This ADR fixes
  the contract it must meet and how a target is admitted for it (D7), not its implementation.

## Decision

### D1 — One registry, additive layers

The registry is `LIONAGI_HOME/hooks/agents.json`. It is an assembly over the hook library, in the
same attachment shape the Operator already uses, plus four fields: `required`, `engines`, `args`
and `requires` (D2).

```json
{
  "version": 1,
  "attachments": [
    {"hook": "redact", "event": "agent_prompt", "required": true,
     "engines": ["claude", "codex"], "args": ["--hook", "prompt", "--engine", "{engine}"]},
    {"hook": "redact", "event": "pre_tool", "required": true,
     "engines": ["claude", "codex"], "args": ["--hook", "pre-tool", "--engine", "{engine}"],
     "requires": ["pre_tool.deny", "pre_tool.rewrite", "pre_tool.fail_closed", "all_tools_gated"]},
    {"hook": "redact", "event": "post_tool", "required": true,
     "engines": ["claude", "codex"], "args": ["--hook", "post-tool", "--engine", "{engine}"],
     "requires": ["post_tool.block"]},
    {"hook": "redact", "event": "claude:Stop", "required": true,
     "engines": ["claude"], "args": ["--hook", "stop", "--engine", "claude"]},
    {"hook": "redact", "event": "codex:Stop", "required": true,
     "engines": ["codex"], "args": ["--hook", "stop", "--engine", "codex"]},
    {"hook": "redact", "event": "agent_exit", "required": true,
     "engines": ["claude", "codex"], "args": ["--hook", "exit", "--engine", "{engine}"]}
  ]
}
```

A library entry that the registry attaches carries `argv` (a non-empty list of non-empty strings,
the rule ADR-0048 D4 applies to external hook commands; ADR-0120 D6 types `command` as argv) and may
carry `pin`, a map from file path to SHA-256 that must match at every spawn:

```json
{"hooks": {"redact": {
  "description": "tokenize secret values before the engine holds them",
  "argv": ["/opt/hooks/bin/redact"],
  "pin": {"/opt/hooks/bin/redact": "<sha256>"},
  "timeout": 10
}}}
```

Exact semantics:

- The attachment's `argv` is the library entry's `argv` followed by the attachment's `args`.
  `{engine}` is the only substitution, replaced by the target engine's name; any other `{...}` in
  `args` is refused at load.
- A library entry with only the legacy string `command` cannot be attached here: its quoting is
  ambiguous and ADR-0120 D6 requires argv. The Operator's existing string entries keep working for
  the Operator until they are translated, as ADR-0120 D6 (the legacy shell executor is "translated/
  deprecated and then deleted") and its Phase 5 (external schema authority) provide.
- The library schema gains `argv` and `pin`. Today the library validator accepts only
  `description`, `command` and `timeout` on an entry, and one refused entry fails the whole
  library, which for the Operator is a hook-less turn (P4). The schema change therefore lands
  before, or with, the first library entry that carries either field.
- Trust tier. Registry and library entries are user-authored in ADR-0048 D7's terms: they live
  under `LIONAGI_HOME` and are written by the operator, by hand or through the Studio hooks API.
  Neither `li hooks import` nor a plugin bundle writes them, and this record adds no import path
  into them, so ADR-0048 D7's `trusted_hook_commands` approval does not gate them. `pin` is not a
  trust record. It checks that the files an entry runs are still the files the operator pinned, and
  it is checked when a spawn is compiled, not when the engine later runs the hook for each event.
  D6 therefore also refuses a pinned file, or its directory, that anyone other than its owner can
  write.
- `engines` absent means every CLI engine lionagi spawns. D8 decides what that does to an engine
  with no renderer.
- `required: true` marks the baseline. Profiles and the Operator may add attachments. No layer
  may remove a required attachment, narrow its matcher, change its arguments, or disable it. There
  is no `enabled: false` at the registry level; an attachment that should not run is deleted from
  the file.
- Layers, in order: the registry, then the agent profile's `hooks` assembly (which stops being a
  warning and starts applying), then the Operator's assembly for Operator spawns. Duplicates, equal
  on (hook, event, matcher, argv), collapse to the first.
- The automated PR review pipeline's agents (ADR-0094) use the registry as is. There is no
  daemon-scoped overlay; if a later need is shown for one, it is another additive layer under the
  same no-removal rule.
- Swapping the hook implementation is an edit to the library entry's `argv` and `pin`. Once the
  schema change above has landed, no lionagi code changes.

Why this way. The library already holds definitions by name and the Operator already attaches by
name, so the registry adds no second definition surface. A separate file, rather than a scope key
inside `operator_hooks.json`, keeps two contracts apart: the Operator's assembly is an operator's
preference, edited over the Studio API, while the agent registry is a baseline no later layer may
subtract from. Profiles stay additive because a profile is chosen by the caller of a spawn, and a
baseline the caller can opt out of is not a baseline.

### D2 — Events, engine labels and capabilities

The neutral events stay as they are (`pre_tool`, `post_tool`, `prompt_submit`, `session_start`,
`session_end`). An event that exists only in one engine is written with that engine's label, as
ADR-0120 D6 requires: `claude:Stop`, `codex:Stop`. The library's current `post_response -> Stop`
mapping presents Claude's `Stop` as neutral; this registry does not accept `post_response`, and the
library mapping follows ADR-0120 Phase 5. Two events are new and are lionagi's own, not an
engine's: `agent_prompt` and `agent_exit` (D7). lionagi fires both for every engine, so every
adapter lists them among its events.

Each engine adapter publishes a capability table keyed by the engine's version:

```python
@dataclass(frozen=True, slots=True)
class EngineHookCapabilities:
    engine: Literal["claude", "codex"]
    version: str                     # exact version string the conformance suite ran against
    events: frozenset[str]           # events the adapter renders, plus agent_prompt and agent_exit
    capabilities: frozenset[str]     # see below
    settings_keys: frozenset[str]    # caller settings keys the Claude render may merge (D4)
```

| Capability | Meaning |
|---|---|
| `pre_tool.deny` | a pre-tool hook can refuse a call, and the model sees the hook's reason as the call's result |
| `pre_tool.rewrite` | a pre-tool hook can replace the call's input before it runs |
| `pre_tool.fail_closed` | when a pre-tool hook crashes, times out or returns output the engine cannot parse, the call does not run |
| `post_tool.block` | a post-tool hook can replace what the model sees with its own reason |
| `turn_end` | a hook runs when the agent's turn ends |
| `all_tools_gated` | every tool that returns bytes to the model passes the pre-tool hook, including MCP tools and incremental input to a running command |

An attachment may declare `requires: [<capability>, ...]`. The table for a version contains only
what the conformance suite (Acceptance A1-A4) showed at that version, and `settings_keys` only keys
the suite showed run no command. A version with no table has no capabilities, so a spawn of it
refuses every attachment that requires one. Reading the version: the adapter runs the resolved
engine binary with `--version` once and caches the answer by the binary's resolved path, inode, size
and modification time.

Rollout order. The conformance suite and the first capability table land before `agents.json`
carries any required attachment. A required attachment against a version with no table refuses
every spawn of that engine on the machine, so the other order stops every agent until the suite
catches up. The suite's first run also records whether a Claude agent with empty setting sources
still discovers `CLAUDE.md` files (A4), and D4 states the measured answer, never an assumed one.

Why this way. The alternative of adding `stop` to the neutral vocabulary with a per-target check
would make a Claude-specific behavior look portable, which ADR-0120 D6 already rejected. Dropping
the stop event from the redaction entry would remove the only point at which a transcript can be
scanned before the engine's process ends. And without a capability table the registry can only say
that a hook was attached, never what attaching it achieved, which is P7.

### D3 — One compiler, required at the spawn

One function compiles the layers for one spawn:

```python
def compile_agent_hooks(
    *,
    engine: Literal["claude", "codex", "gemini", "pi"],
    engine_version: str,
    cwd: Path,
    run_id: str,
    profile_attachments: Sequence[Attachment] = (),
    operator_attachments: Sequence[Attachment] = (),
    snapshot: HooksSnapshot | None = None,
) -> RenderedHooks: ...

@dataclass(frozen=True, slots=True)
class RenderedHooks:
    engine: str
    snapshot: HooksSnapshot          # registry + library content and digest, read once per run
    argv_additions: tuple[str, ...]  # flags the spawn appends
    files: tuple[Path, ...]          # files the spawn owns and removes when the run ends
```

Exact semantics:

- Each CLI provider's `_ndjson_from_cli` calls `compile_agent_hooks` after the request is final and
  before it builds `cmd`, and passes the result to `ndjson_from_cli()`, unless the request already
  carries a `RenderedHooks` (the continuation below), which it passes instead.
- `ndjson_from_cli()` gains a keyword-only `hooks: RenderedHooks | NoAgentHooks`. A call without it
  raises `TypeError`. `NoAgentHooks` exists for non-agent subprocesses that share the function and
  is refused when the command's first element resolves to an agentic engine binary: the resolved
  path equals the resolved binary of one of lionagi's CLI providers, today `claude`, `codex`,
  `gemini` and `pi`. A new provider, or a new path to an existing one, therefore cannot spawn
  without passing the compiler.
- `run_id` is a fresh identifier the compiler mints for each provider call, so two calls never
  share a render path. A provider call is one request handed to a provider, including any
  continuation the provider starts on its own.
- The snapshot is read once per provider call, unless the caller passes one (the fan-out below).
  The request carries the compiled `RenderedHooks` as a runtime-only field, excluded from
  serialization like `env` and `on_spawn`. The `auto_finish` continuation restores that field by
  identity on its copy, as it already does for `env` and `on_spawn`, so it reuses the first turn's
  render, compiles nothing and creates no second file; an edit to the registry during the call does
  not change the continuation's hooks. A resumed `li agent` run is a new provider call and compiles
  from the registry as it is then.
- A fan-out compiles each worker's spawn from the snapshot taken at the fan-out's admission, so a
  bad edit made while it runs refuses no worker that was already admitted.
- The compiler is the only reader of `agents.json`. Nothing else interprets it.

Why this way. Builders are many and miss the continuation (P3); a wrapper around the spawn could be
bypassed by any code that calls the provider directly. The spawn function is the one place every
agent passes, and making the render a required argument of that function turns "every path goes
through it" into a type error for the path that does not.

### D4 — Claude render

- The spawn appends `--setting-sources ""` and `--settings <file>`, where `<file>` is
  `LIONAGI_HOME/run/hooks/<run_id>-<snapshot digest, 12 hex>.json`, created with exclusive create
  and mode 0600, holding `{"hooks": ...}` rendered from the compiled attachments. An existing file
  at that path is a refusal, never an overwrite. The `auto_finish` continuation passes the same
  file (D3) and writes none.
- The rendered path is absolute and outside the repository. It is not a request field: the
  repository-containment rule (P5) guards paths a caller supplies, and this one is produced by the
  compiler after validation. `LIONAGI_HOME` is set by the environment, so the compiler checks the
  resolved path and refuses a render that would land under the agent's working tree (D6).
- A caller-supplied `settings` file is still accepted when every top-level key it holds is in the
  `settings_keys` the adapter publishes for that engine version (D2). The compiler merges those keys
  into the rendered file, so the spawn passes exactly one `--settings`. `hooks`, `disableAllHooks`
  and any key that makes the engine run a command are never in that set, and a file holding any key
  outside it is refused.
- A caller-supplied `setting_sources` other than empty is refused. A spawned agent never reads the
  user, project or local settings files.
- Each attachment's argv becomes the hook's `command` string by `shlex.join`, so the shell the
  engine runs it through sees exactly that argv. ADR-0120 D6 keeps lionagi's own hook executor off
  the shell ("`command` is argv and never reaches a shell"); Claude's hook `command` is a string the
  engine runs through a shell, so this is the one place an argv is turned into shell text, by
  quoting alone, at the engine boundary. Nothing else in the render is shell text.
- The render's files are removed when the provider call returns, after any continuation.

What an agent loses with setting sources empty: user and project permissions, environment entries,
model defaults, plugin enablement and hooks. Whether the CLI still discovers `CLAUDE.md` files is a
property of the engine version; Acceptance A4 measures it and the implementing PR records the
answer here. Anything an agent needs from those sources is passed explicitly by lionagi, as MCP
servers already are.

Why this way. Empty setting sources is the only switch that keeps both the user's interactive file
and a repository's own files out, and it already works for the Operator. A file rather than an
inline JSON string keeps the rendered hooks out of the process's argv and gives A5 a digest to read
back (Alternatives). Merging the caller's file through an allow-list, rather than refusing caller
files or filtering by a deny-list, keeps today's callers working while a key a later engine version
adds, possibly one that runs a command, stays out until the suite has looked at it.

### D5 — Codex render

- The spawn passes `--ignore-user-config`, so the user's `config.toml` and `hooks.json` are not read.
  `CodexCodeRequest.ignore_user_config` (#3671) carries the flag; for an agent spawn the compiler
  sets it and a caller cannot unset it.
- The flag empties the user layer only: a project layer still loads when a trust source outside the
  user config marks the project trusted. The change in #3671 walks the same layers Codex would, from
  the working directory to the project root (`lionagi/providers/openai/_codex_project_layers.py`),
  and refuses a flagged spawn when any of them declares MCP servers. The compiler extends that refusal
  to a layer that declares hooks (inline or a `hooks.json`). When the set of layers cannot be
  established (an unreadable directory, a symlinked root outside the tree), the spawn refuses.
- The rendered hooks ride as `-c` overrides in one inline layer, generated from the compiled
  attachments.
- Trust: the adapter computes Codex's trust record for each rendered hook and passes it in the same
  layer, in the form the pinned Codex version reads. A Codex version whose trust scheme the adapter
  does not know has no capability table (D2) and refuses any required attachment. The bypass flag
  is never passed.

Why trust records and not the bypass flag. The flag trusts every enabled hook in the invocation,
whatever its source. With records, a source lionagi failed to detect (a layer the walk missed, a
plugin, a source a later Codex version adds) still meets Codex's own trust step and does not run.
The records' weakness is coupling to Codex's hash scheme; the version-keyed table turns a scheme
change into a refusal at the first spawn, rather than hooks that silently stop running.

### D6 — Fail closed

Before the spawn, each of these raises `AgentHooksRefused`, a typed refusal a launcher can tell from
the agent's own failure:

- the registry or library is missing a referenced entry, unreadable, or invalid;
- an attachment names an unknown event, an event its target does not render, or a capability its
  target's table lacks;
- a `pin` digest does not match, a pinned file is missing, or a pinned file or its directory can be
  written by anyone other than its owner;
- an attachment's `argv[0]` does not resolve to an executable file;
- a caller supplied hooks of its own (D4, D5), a settings key outside the version's `settings_keys`,
  or a non-empty Claude setting source;
- a Codex project layer declares hooks or MCP servers, or the layer set cannot be established;
- the render path resolves under the agent's working tree;
- writing the render failed;
- an `agent_prompt` hook failed, timed out or returned output that is not the redacted text (D7).

There is no fallback that spawns without hooks. A refused fan-out worker fails that worker and the
work that depends on it; workers already admitted finish under their snapshot. A scheduled run or
review-pipeline job that is refused records a refusal outcome and is not retried until its
configuration changes. The Operator's current behavior, a hook-less turn on a broken config, is
replaced by the same refusal; the repair path is the Studio hooks API, which starts no agent.

What happens after the spawn is the engine's: a hook that crashes or times out is handled by the
engine's own rules. A hook entry that must hold under those failures requires `pre_tool.fail_closed`
(D2), which the conformance suite grants only after injecting a crash, a timeout and malformed
output (A2); an engine version that runs the tool when its pre-tool hook fails does not receive it,
so a spawn of that version with the redaction entry is refused here.

Why this way. Each check above is one whose failure would otherwise start an agent with fewer hooks
than its configuration says, which is the silent removal P4 records. Refusing before the spawn costs
a run; a hook-less run of a redaction entry costs the secret, and the codebase already judged a
guard that silently does not run to be worse than none (P2). The repair paths (editing the
registry or library, the Studio hooks API) start no agent, so a refusal never blocks its own repair.

### D7 — The redaction entry's contract

What "at transcript write time" means here. Neither engine is known to call a hook before it
persists a tool result. The only points before any raw bytes exist inside the engine are lionagi's
own hand-off of the prompt and the pre-tool hook. So the redaction entry protects the context and
the transcript by substitution at those two points:

- `agent_prompt`: fired by lionagi, not by the engine, in the provider's `_ndjson_from_cli` on the
  final request, before the spawn, for every spawn including the `auto_finish` continuation. It
  receives every text lionagi hands the engine as model input (the prompt, a system prompt or its
  appendix, and the earlier turns lionagi replays when it resumes no engine thread) and returns it
  redacted; the spawn carries the returned text. Its failure refuses the spawn (D6).
- `pre_tool` (requires `pre_tool.deny`, `pre_tool.rewrite`, `pre_tool.fail_closed`,
  `all_tools_gated`): for each call, the hook either arranges that the result passes its redactor
  before the engine receives it (it performs the read itself and returns the redacted text as the
  deny reason, or rewrites a shell command so its output passes through the redactor), or it denies
  the call. A call it cannot route through the redactor is denied, never allowed. The engine then
  never holds a raw value, so it has none to persist.
- `post_tool` (requires `post_tool.block`): checks the result the model is about to see and blocks
  it if a raw value is present. This protects the model's view when substitution missed; it does
  not undo what the engine already wrote.
- `claude:Stop` / `codex:Stop`: scans the session's transcript and scrubs any raw value, and reports
  failure if it found one.
- `agent_exit`: fired by lionagi, not by the engine, from `ndjson_from_cli()` after the child exits
  for any reason (success, error, signal, timeout, cancellation). It receives the engine, the
  engine's session id from the stream, and the transcript path the adapter derives from it. It runs
  the same scan and scrub, so an engine that died before `Stop` is still scrubbed. It runs under the
  library entry's `timeout`; a timeout, a crash or a non-zero exit is a failure and marks the run
  failed. A cancelled run waits for the scan, shielded from the cancellation, up to that bound, and
  then reports the run failed with the scan unfinished.

The guarantee holds for lionagi's own prompt text and for every call the pre-tool hook can see.
`all_tools_gated` is the capability that says the hook sees every call, and the conformance suite
is the only thing that grants it, per engine version. A value that reaches the engine by neither
path (an instruction file the engine loads on its own, say) is found by the stop and exit scans of
the engine's transcript, not prevented; the post-tool hook sees tool results only. lionagi's own
stores (its stored messages, the stream log) receive what the model then writes as the engine
emitted it, and no hook scrubs them.

Why this way. lionagi's prompt hand-off and the pre-tool hook are the only points at which the
engine has not yet held the bytes, so substitution there is the only mechanism that keeps a value
out of the context and the transcript rather than removing it afterwards. The prompt is lionagi's
own text, so lionagi redacts it itself and needs no engine hook for it. Denial is allowed beside
substitution because a call the hook cannot route through the redactor must fail closed, and
refusing it is the only closed outcome. The later scans stay because substitution can miss, and a
miss found and scrubbed at stop or exit is better than a miss kept. The exit event is lionagi's own
because only the spawning process sees every way an engine run ends.

### D8 — Scope

- In scope: every process lionagi spawns through `ndjson_from_cli()` whose binary is `claude` or
  `codex`, by every path in P3, including the Operator, the `auto_finish` continuation and resumed
  turns.
- `gemini` and `pi` agents spawn through the same function but have no renderer. An attachment whose
  `engines` includes them, or names no engines, refuses their spawn when it is `required`. A
  registry that wants those engines to run lists `["claude", "codex"]` explicitly, which records
  that they run without that hook.
- API-model agents run lionagi's own tool loop and are not CLI processes. Rendering this registry
  into the ADR-0048/ADR-0120 in-process seam is DEFERRED: the target design is an adapter that
  compiles the same attachments into `ExternalHookProfile(profile="lionagi")` registrations, under
  the same no-removal rule, with its own capability table.

Why this way. Scope follows the spawn function because that is where D3's requirement can be
enforced. `gemini` and `pi` are refused rather than silently skipped for the same reason as D6: a
required entry that does not run is the failure this record removes, and listing the engines
explicitly turns "runs without that hook" into a visible line in the registry. The in-process seam
is deferred because its events are lionagi's own tool loop, already governed by ADR-0048 and
ADR-0120 under a different executor, and rendering into it needs its own conformance table rather
than a borrowed one.

## Acceptance

The implementing PRs carry these, each with the artifact that shows it.

- **A1 — redaction end to end.** A fixture of secret values, given in the prompt and read through
  tools by a Claude agent and a Codex agent that lionagi spawned, comes back as tokens. Zero raw
  values in: what the model saw, the engine's native transcript or rollout, lionagi's own stored
  messages, the stream log, stderr, temporary and spilled files, and the run's artifacts. Checked
  during the run, not only after `Stop`. Only the fixture's input file, the token vault and
  lionagi's record of the caller's instruction as the caller sent it are excluded.
- **A2 — failure injection.** A failing `agent_prompt` hook; pre-tool hook timeout, crash and
  malformed output (the evidence for `pre_tool.fail_closed`); an exit hook that times out; the
  engine killed mid-run, a resumed run, a Claude `auto_finish` continuation, two concurrent runs, a
  registry edited during a run, a missing hook command, an unsupported event. Each either refuses
  before the spawn or leaves zero raw values, and the exit-hook timeout marks the run failed.
- **A3 — every tool path.** Native file reads, a shell command writing to stderr, incremental input
  to a running command, an MCP tool, and a nested agent call, each checked for what the model saw
  and what was persisted, separately.
- **A4 — isolation canaries.** A hook in the user's Claude settings, a hook in a repository's
  `.claude/settings.json`, a Codex user `hooks.json`, a Codex project layer with hooks, a symlinked
  project root and an organization-managed setting. The first five never run (the Codex project
  layer refuses the spawn); the sixth is recorded. The implementing PR also records whether
  `CLAUDE.md` is still discovered with setting sources empty (D4).
- **A5 — every path renders.** One agent started by each path in P3 (`li agent` new and resumed, flow,
  fan-out worker and synthesis, play, each MCP submit verb, an agent schedule, the review pipeline,
  the Operator, an engine agent and an orchestration-pattern agent built by `create_agent`, and a
  Claude `auto_finish` continuation) shows the rendered flags in its own argv, read from the running
  process, and the rendered file's digest matches its snapshot.

## Consequences

- Every Claude and Codex agent lionagi starts runs the same baseline, declared in one file. Changing
  the redaction implementation is a library edit.
- Spawned agents stop seeing the user's interactive engine configuration. Anything they relied on
  from it (permissions, plugins, MCP servers, model defaults) has to be passed by lionagi
  explicitly, and some will be found missing after the change. That is the intended direction;
  the cost lands once.
- Agents become refusable for configuration reasons. A broken registry stops every spawn on the
  machine until it is repaired, the Operator included. A hook guard that silently stops guarding is
  the failure this trades away.
- An engine upgrade stops the agents that require a capability until the conformance suite runs
  against the new version. Pinning engine versions becomes part of operating lionagi.
- `ndjson_from_cli()` gains a required argument; every caller changes once.
- Reversal: D1-D3 are cheap to reverse (the registry can hold zero attachments). D4 and D5's
  isolation is the expensive part to reverse, because once agents run without the user layer,
  configurations that only worked through it are gone from use.

## Alternatives considered

### Scopes inside `operator_hooks.json` (operator / agents / review)

One file, one API. It loses because the Operator's assembly and the agent baseline have different
rules: the first is a preference that fails a turn today only when its own owner breaks it; the
second is a floor nothing below it may remove. One file with per-scope rules invites the scope that
is easiest to edit becoming the one that decides.

### Hooks declared per profile, with a global default

Profiles already carry a `hooks` key. As the only surface, a profile could replace or omit the
default, and an agent spawned without a profile (`li agent` with a bare model, a fan-out worker)
would need a separate rule. Profiles are kept as an additive layer (D1).

### A daemon-scoped overlay for the review pipeline

It would let the review pipeline tune its hooks for latency. No conflicting requirement has been
shown, and an overlay that can subtract weakens the floor for exactly the agents that read untrusted
pull-request text. Deferred until a measured conflict exists; it would then be additive.

### Render in the model builders

Three construction sites (`build_imodel_from_spec`, `build_chat_model`, `create_agent`) cover most
spawns and are where per-role context is. They miss the Claude `auto_finish` continuation, which
copies the request after the builder ran, and a resumed run that rebuilds its request from storage.
Rejected for D3.

### Codex hook trust by the bypass flag

No coupling to Codex's hash scheme. Rejected because it trusts every enabled hook in the invocation
(D5): the protection would then rest entirely on lionagi's project-layer walk matching Codex's
discovery exactly, in every version.

### Keep the Operator's fail-open behavior for agents

A broken config would not stop work. For a redaction hook it means a run that leaks while its
configuration says it does not, and P2 already records the codebase's own judgment that such a guard
is worse than none.

### `post_response` as a neutral stop event

Shorter attachment lists. ADR-0120 D6 rules out presenting Claude's `Stop` as provider-neutral, and
the two engines' stop events are not shown to carry the same contract.

### Inline `--settings` JSON instead of a file

Claude's `--settings` is documented to take a JSON string as well as a path. It avoids the render
file and its cleanup. It loses on inspectability and on argv size: the rendered hooks would sit in
the process's argv, and A5 compares a file digest against the snapshot, which an argv string makes
harder to read back. Kept as a fallback if the file path proves unworkable.

## Notes

- Measured at lionagi `main` 208bcc08: no spawn path other than the Operator sets
  `setting_sources`; `CodexCodeRequest` has no user-config isolation field; the library maps
  `post_response` to `Stop`. The change in #3671 (open) changes the first two: flow and fan-out
  workers run Claude with empty setting sources and Codex with `--ignore-user-config`, and the
  field exists. This record builds on that change and merges after it.
- Engine behaviors this record relies on (Claude's `--setting-sources`, Codex's `--ignore-user-config`,
  project layers, hook trust records, hook decision shapes) are taken from each vendor's
  documentation and are not yet measured at a pinned version. D2's capability tables exist so that
  nothing depends on them until the conformance suite has measured them.
