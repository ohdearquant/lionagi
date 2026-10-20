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
declare the hooks those processes run, in one registry with additive layers, in place of the hook
files the user and the repository keep, and makes every agent turn lionagi starts through its CLI
providers, which all pass one spawn function, compile those hooks and carry them; a turn that
cannot is refused before it starts (D3, D6). Every spawn applies its declared hooks, and a
deployment that declares a hooks requirement cannot silently lose its required baseline on any of
lionagi's own launch paths (D1). Other engines lionagi spawns have no renderer; a required entry
that names them or omits `engines` refuses their spawn. Processes that do not pass the spawn
function, such as a command an agent's own shell tool runs, are outside this record (D8), and so
is what an engine adds from its own managed policy.

The first hook it carries serves one redaction requirement: the values in the run's secret set
(the values the hook is configured with, and the named part of what lionagi's own log redactor
treats as secret in what it hands the engine process: values under declared names, and values of
at least eight characters under variable and flag names that match its credential words, D7) are
replaced by tokens from the run's redaction table before they reach the model's context
and before the engine writes its transcript. D7 meets it on the routes lionagi mediates. lionagi
redacts the prompt text it hands the engine before the spawn: the hook replaces each value it is
configured with by its token, and a spawn whose prompt text still holds a value of the table is
refused. A tool call runs only on a route that redacts its output before the
engine receives it (a read the hook performs under the run's read policy, a shell command whose
output passes the redactor, an MCP call through a redacting proxy bound to the run), and every
other tool is denied, because neither engine is known to call a hook before it persists a tool
result. The post-tool hook blocks a result the model would see with a raw value in it; the stop and
exit scans look for every value in the run's secret set in the engine's transcript, the exit scan
scrubs what it finds, and a find fails the run; no hook scrubs lionagi's own stored messages or
stream log. A value that reaches the engine by no mediated route, such as an instruction file the
engine loads on its own, is not prevented: the scans find it only when the engine writes it into
its transcript, which A1 measures. The entry guards an agent that is not trying to defeat it: a
shell command runs with the account's authority, and what it does besides print is outside the
entry, and the redactor knows a value only by its exact text, so a value printed encoded or split
passes it (D7).

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
agent runs no hooks from the user, project or local sources and a Codex agent runs only the hooks of
a trusted project layer
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
| Where a hook for a spawned agent is declared | D1: one registry, `LIONAGI_HOME/hooks/agents.json`, over the existing library; later layers add, never remove; a deployment requirement held outside `LIONAGI_HOME` makes the baseline mandatory; every attached command needs an approval of its final argv, except the Operator's legacy string entries until they are translated |
| Event vocabulary and what an entry may require | D2: neutral events plus engine-labeled events per ADR-0120 D6; each target publishes a capability table proven by conformance; entries declare `requires` |
| Where the render happens | D3: one compiler, required by each CLI provider's spawn; `ndjson_from_cli()` takes the render as a required argument and starts only the binary it was compiled for |
| Claude render | D4: a per-call settings file under `LIONAGI_HOME`, emitted by the spawn, with setting sources empty and only lionagi's MCP servers |
| Codex render | D5: an inline config layer, user config ignored, hook-declaring project layers refused, trust by computed records |
| Failure policy | D6: every failure before the spawn refuses it with a typed refusal, a missing required registry and a missing approval included; no hook-less fallback |
| What the redaction entry guarantees and how | D7: prevention is lionagi's own prompt redaction plus mediated tool routes (a hook-performed read, redacted shell output, a redacting MCP proxy) with every other tool denied; post-tool blocks, the stop scan counts, and a lionagi-fired exit event scans, scrubs and checks receipts |
| Scope | D8: every Claude and Codex spawn through `ndjson_from_cli()`; other engines refuse required entries they cannot render |

Out of scope:

- lionagi's in-process hooks for API-model agents: ADR-0048 and ADR-0120 own them. Rendering this
  registry into those seams is DEFERRED (D8).
- Programs a schedule's `command` action starts directly: they are outside lionagi. They are covered
  exactly when they start their agents through lionagi (`li agent`), and not otherwise.
- Processes that do not pass `ndjson_from_cli()`: a command an agent's own shell tool runs (an
  API-model agent's Bash tool can run `claude -p` or `codex exec`), the agent factory's
  `codex mcp list --json` probe, which lists servers and starts no agent turn, and D2's version
  probe. Under a redaction entry the shell route (D7) admits such a command like any other: its
  output passes the redactor, and an engine it starts directly runs outside any render, a residual
  D7 names. A `li agent` it runs does pass `ndjson_from_cli()`, renders that process's own
  registry and checks it against the requirement the engine's environment hands it, unless the
  command removed it (D1).
- Engine-managed policy (an organization's managed settings) and what an engine adds from its own
  sources (an account's connectors, Codex's system configuration, the MCP servers either may
  declare): lionagi cannot disable it. Acceptance A4 records what it injects; this ADR makes no
  claim over it.
- What the redaction hook does inside its own process: the hook is a separate program. This ADR fixes
  the contract it must meet and how a target is admitted for it (D7), not its implementation.

## Decision

### D1 — One registry, additive layers

The registry is `LIONAGI_HOME/hooks/agents.json`. It is an assembly over the hook library, in the
same attachment shape the Operator already uses, plus four fields: `required`, `engines`, `args`
and `requires` (D2).

Absence and the deployment requirement. An installation that has not declared a registry
requirement may start with no registry and an empty attachment set. Its spawns still apply the
engine isolation of D4 and D5, and each records the resolved registry path and that it was absent,
with a notice on stderr. A present registry is validated and its content digest recorded; an
unreadable or invalid registry is never treated as absent. A deployment that requires hooks
declares a requirement: the expected absolute registry path and its mandatory attachments, each
given by hook, event, matcher, engines, args and `requires`. The launcher names the requirement
file by an absolute path in `LIONAGI_HOOKS_REQUIREMENT`, set by the service definition, the
schedule or the job that starts lionagi, and lionagi never looks for it under `LIONAGI_HOME`, so a
run that resolves a different home still finds it. The compiler reads it in the lionagi process
that spawns the engine; the engine child never reads it. A worker, a fan-out or a continuation in
that process uses the requirement the process read, and each run records it. A lionagi process
that starts another `li` process hands it on in that child's environment: the MCP job runner
(`lionagi/mcp/jobs.py`) passes all of its environment but `CLAUDECODE` today and the scheduler
(`lionagi/studio/scheduler/subprocess.py`) all of it to a `li` child, and the run-resume worker
(`lionagi/studio/services/run_resume_worker.py`) and the MCP verb dispatcher
(`lionagi/mcp/dispatch.py`) start `li` with the inherited one. Each of the four sets
`LIONAGI_HOOKS_REQUIREMENT` explicitly after building the child's environment, so a later filter
on that environment cannot drop it. `ndjson_from_cli()` sets it the same way in the engine
child's environment, which the engine does not read and the commands its shell runs inherit, so a
`li` command an agent's shell runs reads the same requirement unless that command removes it,
which nothing here prevents (D8): whoever controls a launcher's environment controls its
requirement. A process started outside these paths carries only what its own environment holds.
No profile or request can weaken it. A registry at a different path, an absent required
registry, or a missing or weakened mandatory attachment refuses the spawn (D6, A6). An empty registry
satisfies only a requirement whose mandatory set is empty, and first use never creates a registry.
The compiler checks the registry against the requirement; the requirement is never a second source
the render is built from.

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
the rule ADR-0048 D4 applies to external hook commands; ADR-0120 D6 types `command` as argv), whose
first element is an absolute path, so the executable that runs does not depend on the engine's
`PATH` or working directory. It carries `pin`, a map from file path to SHA-256 that must match at
every spawn, when a required attachment names it, and may carry one otherwise (below):

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
- A library entry with only the legacy string `command` cannot be attached by the registry or a
  profile: its quoting is ambiguous and ADR-0120 D6 requires argv. The Operator layer keeps
  attaching its existing string entries, rendered verbatim as Claude's `command` string as today,
  with a deprecation notice on each turn that attaches one, until ADR-0120 Phase 5 (external schema
  authority) translates them; ADR-0120 D6 says the legacy shell executor is "translated/
  deprecated and then deleted". They need no approval in that window, because today they need
  none and this record removes the Operator's fail-open paths without tightening its legacy one.
  They are never `required`, and a spawn whose baseline holds a required attachment for its engine
  refuses them as it refuses any later-layer attachment outside `inert_events` (below), so a
  deployment with a redaction baseline never runs one.
- The library schema gains `argv` and `pin`, `timeout` becomes a positive number of seconds (a
  boolean is refused) that an entry attached to `agent_prompt`, `pre_tool` or `agent_exit` must
  carry, and the Operator's materializer changes with it.
  Today the library validator accepts only `description`, `command` and `timeout` on an entry, and
  one refused entry fails the whole library: an entry carrying `argv` fails `read_library()` with
  `HookLibraryError`, which `materialize_settings_file` catches and turns into a hook-less Operator
  turn (P4). Once the validator accepts `argv`, the materializer, which reads `command` alone,
  would raise `KeyError` on an argv-only entry, which the engine call site's catch-all turns into
  the same hook-less turn. The materializer therefore renders an argv entry as D4 does
  (`command` = `shlex.join(argv)`), and any failure to materialize, of any exception type, refuses
  the Operator turn (D6). Both changes land before, or with, the first library entry that carries
  either field.
- Command approval. Registry and library files take no trust tier from where they live: ADR-0048 D7
  assigns tiers to hook entries in its own settings files, by provenance and defining file, and
  these files are neither. Every command attached to a spawned agent, by the registry, a profile or
  the Operator, needs an explicit approval of its final argv: the library entry's `argv` followed by
  the attachment's `args`, after `{engine}` is substituted. The approval is ADR-0048 D7's trust
  record (`compute_trust_record` in `lionagi/hooks/external.py`), kept in `trusted_hook_commands` in
  the user settings file that rule names: the argv hash (`sha256(json.dumps(argv))`) identifies the
  request, and the resolved executable path and that executable's content digest identify what runs.
  All three must match at every spawn, as the loader of that rule already requires, so a different
  executable at the same path needs a new approval. It is recorded through the same trust workflow,
  `li hooks trust`, which today lists only commands `li hooks import` wrote and gains every concrete
  argv the registry and its layers attach. A project's settings file cannot supply the approval, and
  the compiler never records one. A missing approval, an approval store that cannot be read, or a
  changed argv, path or digest refuses the spawn (D6, A7). Codex trust records (D5) are generated
  only after this check and do not replace it. An entry that carries imported provenance needs the
  same approval, never a weaker one. The Operator's legacy string entries are the one exception,
  above.
- `pin` and the owner-only checks are integrity checks beside the approval. The approval binds the
  executable; a pin binds the other files an entry runs, such as the script an interpreter reads.
  A library entry that a `required: true` attachment names must carry `pin`, and the pin must
  cover every element of its argv that is an absolute path to an existing file other than
  `argv[0]`. Both are checked when a spawn is compiled, not when the engine later runs the hook for
  each event. D6 also refuses `argv[0]`, a pinned file, the registry, the library, the approval
  store or the requirement file, or a directory holding one, that anyone other than its owner can
  write. Neither freezes a file: a process of the same account, an agent's own shell command
  included, can change it between the compile and an event, and the next compile is where the
  check sees the change (Consequences).
- `engines` absent means every CLI engine lionagi spawns. D8 decides what that does to an engine
  with no renderer.
- `required: true` marks the baseline. Profiles and the Operator may add attachments. No layer
  may remove a required attachment, narrow its matcher, change its arguments, or disable it. There
  is no `enabled: false` at the registry level; an attachment that should not run is deleted from
  the file.
- Layers, in order: the registry, then the agent profile's `hooks` assembly (which stops being a
  warning and starts applying), then the Operator's assembly for Operator spawns. Duplicates, equal
  on (hook, event, matcher, argv), collapse to the first.
- In a spawn whose baseline holds a required attachment for its engine, a later layer may add an
  attachment only on an event the engine's table lists in `inert_events` (D2), an event whose hooks
  can neither change a tool's input nor add to the model's context. Until the conformance suite
  lists one, later layers add nothing to such a spawn, and a layer that tries refuses it (D6). An
  added rewriter could otherwise undo the redaction route after it ran, or an added context hook
  hand the model a raw value, with every required attachment still present.
- The automated PR review pipeline's agents (ADR-0094) use the registry as is. There is no
  daemon-scoped overlay; if a later need is shown for one, it is another additive layer under the
  same no-removal rule.
- Swapping the hook implementation is an edit to the library entry's `argv` and `pin` and a new
  approval of the changed argv. Once the schema and materializer changes above have landed, no
  further lionagi code changes.

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
    project_layer_keys: frozenset[str]  # keys a Codex project layer may set (D5)
    inert_events: frozenset[str]     # events whose hooks cannot change a tool's input or add model context (D1)
```

| Capability | Meaning |
|---|---|
| `pre_tool.deny` | a pre-tool hook can refuse a call, and the model sees the hook's reason as the call's result |
| `pre_tool.rewrite` | a pre-tool hook can replace the call's input before it runs |
| `pre_tool.fail_closed` | when a pre-tool hook crashes, times out or returns output the engine cannot parse, the call does not run |
| `post_tool.block` | a post-tool hook can replace what the model sees with its own reason |
| `turn_end` | a hook runs when the agent's turn ends |
| `all_tools_gated` | every tool that returns bytes to the model passes the pre-tool hook, including MCP tools and incremental input to a running command, and the hook's input carries the engine's id for the call, which D7's receipts check matches |

An attachment may declare `requires: [<capability>, ...]`. The table for a version contains only
what the conformance suite (Acceptance A1-A4, A8) showed at that version. `settings_keys` and
`project_layer_keys` hold only keys the suite showed run no command, widen no permission, and set
no environment value, credential or endpoint, and `inert_events` only events the suite showed
neither rewrite a tool's input nor add to the model's context. A version with no table has no
capabilities, so a spawn of it refuses every attachment that requires one. Reading the version: the
adapter resolves the engine binary once to an absolute path and runs that path with `--version`,
with no shell, the spawn's environment, `LIONAGI_HOME` as its working directory (so no
repository's configuration sits in it), an empty stdin and a 10-second bound. That probe starts no
agent turn and does not pass `ndjson_from_cli()` (D8). The answer is cached by the path, device,
inode, size, modification time and status-change time, and the spawn runs that same path (D3), so
the table chosen is the table of the binary that runs. The status-change time is in the key because
any write to the file moves it and an unprivileged process cannot set it back, while a modification
time can be restored by any process that may write the file. A process of the account that
replaces the binary between the spawn's check of the key and its exec is seen at the next spawn,
not this one, the same window D1 names for pinned files.

Rollout order. The conformance suite and the first capability table land before `agents.json`
carries any required attachment. A required attachment against a version with no table refuses
every spawn of that engine on the machine, so the other order stops every agent until the suite
catches up. The suite's first run also records whether a Claude agent with empty setting sources
still discovers `CLAUDE.md` files (A4), and D4 records that answer when A4 runs, never an assumed
one.

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
    engine_binary: Path,             # absolute; the path whose version was read (D2)
    engine_version: str,
    cwd: Path,
    requirement: HooksRequirement | None,  # the launcher's requirement (D1), or none declared
    profile_attachments: Sequence[Attachment] = (),
    operator_attachments: Sequence[Attachment] = (),
    snapshot: HooksSnapshot | None = None,
) -> RenderedHooks: ...

@dataclass(frozen=True, slots=True)
class RenderedHooks:
    engine: str
    binary: Path                     # the only first element ndjson_from_cli() accepts
    run_id: str                      # minted by the compiler, one per provider call
    snapshot: HooksSnapshot          # registry path, content and digest (or its absence), read once per provider call
    argv_additions: tuple[str, ...]  # flags the spawn appends
    files: tuple[Path, ...]          # the render file and D7's run files, removed when the provider call returns
```

Exact semantics:

- Each CLI provider's `_ndjson_from_cli` calls `compile_agent_hooks` after the request is final and
  before it builds `cmd`, and passes the result to `ndjson_from_cli()`, unless the request already
  carries a `RenderedHooks` (the continuation below), which it passes instead.
- `ndjson_from_cli()` gains a required keyword-only `hooks: RenderedHooks`; a call without it
  raises `TypeError`. There is no opt-out value: every caller of the function under `lionagi/` is a
  CLI provider's `_ndjson_from_cli` (four today: Claude, Codex, Gemini and Pi), and a subprocess that
  is not an agent turn uses another function. The test suite also calls it directly (52 calls in
  8 files at `main` 208bcc08); those calls change once, to pass a render built through
  `compile_agent_hooks` with an empty registry.
- `RenderedHooks` is built only by `compile_agent_hooks`: its constructor takes a module-private
  token, and `__post_init__` raises when the token is missing, so a hand-built object naming another
  binary (`/usr/bin/env`, say) is refused before `cmd[0]` is compared. This guards against a caller
  that builds one by mistake; Python code in the same process can still reach the token, which is
  outside this record's threat (D7).
- `ndjson_from_cli()` refuses a command whose first element is not `hooks.binary`, so the binary
  that starts is the one the render and its capability table were compiled for, and a wrapper in
  front of it (`env claude ...`, a shell string) is refused. A
  new provider therefore cannot call the spawn function without a render; code that starts an
  engine some other way does not pass the function and is outside this record (D8), and A5
  enumerates the function's callers so that a new one is seen.
- `run_id` is a fresh identifier the compiler mints for each provider call and returns on
  `RenderedHooks`; no caller supplies it, so two calls never share a render path. A provider call
  is one request handed to a provider, including any continuation the provider starts on its own.
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

- The spawn appends `--setting-sources ""`, `--strict-mcp-config` and `--settings <file>`, where
  `<file>` is `LIONAGI_HOME/run/hooks/<run_id>.settings.json`, created with exclusive create and
  mode 0600, holding `{"hooks": ...}` rendered from the compiled attachments. An existing file at
  that path is a refusal, never an overwrite. The `auto_finish` continuation passes the same file
  (D3) and writes none.
- `--strict-mcp-config` makes the MCP servers lionagi passes the only servers the agent loads: the
  engine discovers none on its own, so a repository's `.mcp.json`, whose stdio servers are
  commands, reaches an agent only through lionagi. `li agent` passes one: without `--mcp-config` or
  `--no-mcp-config` it resolves the nearest `.mcp.json` at or above the directory it was launched
  from (`resolve_spawn_mcp_servers`, `lionagi/cli/_mcp_resolve.py`) and passes its servers, so
  under `li agent` a repository's stdio servers start, as on `main`, and `--no-mcp-config` is how a
  run declines them. That resolution is an explicit lionagi input, and this record keeps it. Under a
  redaction entry the servers lionagi passes, these included, are reached through the run's proxies
  (D7). A4 shows both halves. For an agent spawn the compiler sets
  `ClaudeCodeRequest.strict_mcp_config` and a caller cannot unset it.
- The rendered path is absolute and lies under `LIONAGI_HOME`, outside the agent's repository unless
  `LIONAGI_HOME` is inside it, which a spawn under a redaction entry refuses (below). It is not a
  request field: the repository-containment rule (P5) guards paths a caller supplies, and this one
  is produced by the compiler after validation. `LIONAGI_HOME` is set by the environment, so under a
  redaction entry the compiler resolves the run directory, `LIONAGI_HOME/run/hooks`, to its real
  path, every symbolic link followed, and refuses the spawn (D6) when that path, or any ancestor of
  it, holds a `.git` entry, or when the check cannot read one of them, because D7's run files hold
  raw values and a file inside a work tree is one `git add` from a commit. Resolving first matters:
  a `~/.lionagi` that is a link into a dotfiles repository has no `.git` above its own path, only
  above the path it points to. The rule reads the run directory's own ancestors, so where the agent
  starts does not change it. It knows a Git work tree only by a `.git` entry: a work tree that a
  repository elsewhere names through `core.worktree` or `GIT_DIR`, with no `.git` inside it, and the
  work trees of other version control systems (`.hg`, `.jj`, `.sl`) are not seen (Consequences). A
  spawn without the entry writes only its render, which holds hook commands and no secret value, and
  the rule does not apply to it. `LIONAGI_HOME` defaults to `~/.lionagi` (`lionagi/_paths.py`), so a
  home directory that is itself a repository, or a `~/.lionagi` that is one or links into one,
  refuses every spawn under a redaction entry, from any working directory, the Operator's default
  root included, until `LIONAGI_HOME` resolves outside every work tree (Consequences). The rule
  keeps those files out of a repository; it does not stop a process of the same account from writing
  them (D7).
- A caller-supplied `settings` file is still accepted when every top-level key it holds is in the
  `settings_keys` the adapter publishes for that engine version (D2). The compiler merges those keys
  into the rendered file, so the spawn passes exactly one `--settings`. `hooks`, `disableAllHooks`
  and any key that runs a command, widens a permission or sets an environment value, credential or
  endpoint are never in that set, and a file holding any key outside it is refused.
- A caller-supplied `setting_sources` other than empty is refused. A spawned agent never reads the
  user, project or local settings files.
- Each attachment's argv becomes the hook's `command` string by `shlex.join`, so the shell the
  engine runs it through sees exactly that argv. ADR-0120 D6 keeps lionagi's own hook executor off
  the shell ("`command` is argv and never reaches a shell"); Claude's hook `command` is a string the
  engine runs through a shell, so this is the one place an argv is turned into shell text, by
  quoting alone, at the engine boundary. Since `argv[0]` is an absolute path (D1), the shell
  resolves no name. Apart from the Operator's legacy string entries (D1), which are shell text as
  today, nothing else in the render is shell text.
- Under a redaction entry lionagi chooses the engine's session id before the spawn and passes it
  (`ClaudeCodeRequest.session_id`, `--session-id`), so the transcript `agent_exit` scans can be
  found even when the child dies before its first event (D7). A caller-supplied session id is used
  the same way. The `auto_finish` continuation passes the first turn's id as `--resume` alone.
  Today its deep copy keeps `session_id` and also sets `continue_conversation`
  (`lionagi/providers/anthropic/claude_code.py`), so it would send `--session-id` and `--continue`
  beside `--resume`; the implementing PR clears both on the copy, and A2 reads the continuation's
  argv.
- The render file, and D7's run files when the redaction entry applies, are removed when the
  provider call returns, after any continuation and after `agent_exit`.

What an agent loses with setting sources empty: user and project permissions, environment entries,
model defaults, plugin enablement and hooks. Whether the CLI still discovers `CLAUDE.md` files is a
property of the engine version; Acceptance A4 measures it and the implementing PR records the
answer here. Anything an agent needs from those sources is passed explicitly by lionagi, as MCP
servers already are, and under a redaction entry those servers are reached through D7's proxies.

Why this way. Empty setting sources is the only switch that keeps both the user's interactive file
and a repository's own files out, and it already works for the Operator. With the settings files
out, a repository's `.mcp.json` is the one command surface it may still control, and
`--strict-mcp-config` leaves lionagi's own resolution as its only way in, which `--no-mcp-config`
declines (above). A file rather than an inline JSON string keeps the rendered hooks
out of the process's argv and gives A5 a digest to read back (Alternatives). Merging the caller's
file through an allow-list, rather than refusing caller files or filtering by a deny-list, keeps
today's callers working while a key a later engine version adds stays out until the suite has
looked at it (A8).

### D5 — Codex render

- The spawn passes `--ignore-user-config`. Codex documents that the flag skips the user's
  `config.toml`; whether the user's `hooks.json` is skipped too is measured by A4's canary, and a
  version whose user `hooks.json` still runs gets no capability table (D2).
  `CodexCodeRequest.ignore_user_config`, which the change in #3671 adds, carries the flag; for an
  agent spawn the compiler sets it and a caller cannot unset it.
- The flag empties the user layer only: a project layer still loads when a trust source outside the
  user config marks the project trusted. The change in #3671 walks the same layers Codex would, from
  the working directory to the project root (`lionagi/providers/openai/_codex_project_layers.py`),
  and refuses a flagged spawn when any of them declares MCP servers. The compiler extends that
  refusal to a layer that declares hooks (inline or a `hooks.json`) and to a layer that sets a key
  outside the version's `project_layer_keys` (D2). When the set of layers cannot be established (an
  unreadable directory, a symlinked root outside the tree), the spawn refuses.
- The rendered hooks ride as `-c` overrides in one inline layer, generated from the compiled
  attachments. Under a redaction entry the MCP servers in that layer are the run's proxies (D7);
  with the user layer ignored and project-layer servers refused, they are the only servers the
  agent loads.
- Trust: after the command approval of D1, the adapter computes Codex's trust record for each
  rendered hook and passes it in the same layer, in the form the Codex version the table names
  reads. A trust record is the engine's consent to run the hook and never stands in for the
  approval. A Codex version whose trust scheme the adapter does not know has no capability table
  (D2) and refuses any required attachment. The bypass flag is never passed.
- Codex reports its session id on its first event. D7 says what `agent_exit` does when a child
  exits before one arrived.

Why trust records and not the bypass flag. The flag trusts every enabled hook in the invocation,
whatever its source. With records, a source lionagi failed to detect (a layer the walk missed, a
plugin, a source a later Codex version adds) still meets Codex's own trust step and does not run.
The records' weakness is coupling to Codex's hash scheme; the version-keyed table turns a scheme
change into a refusal at the first spawn, and a record that does not match at run time is found by
the receipts check of D7, which fails the run.

### D6 — Fail closed

Before the spawn, each of these raises `AgentHooksRefused`, a typed refusal a launcher can tell from
the agent's own failure:

- the registry or library is missing a referenced entry, unreadable, or invalid;
- a declared requirement (D1) is not met: the registry is absent or at another path, a mandatory
  attachment is missing or weakened, or the file `LIONAGI_HOOKS_REQUIREMENT` names is missing,
  unreadable or invalid;
- an attached command has no approval of its final argv, the approval store cannot be read, or the
  argv, its resolved executable or that executable's content changed since it was approved (D1);
- an attachment names an unknown event, an event its target does not render, or a capability its
  target's table lacks, or a later layer adds an attachment outside `inert_events` to a spawn whose
  baseline holds a required attachment (D1);
- an attached library entry has no `argv`, an `argv[0]` that is not an absolute path to an
  executable file, or, attached to `agent_prompt`, `pre_tool` or `agent_exit`, no positive
  `timeout`;
- a library entry that a required attachment names has no `pin`, or a pin that leaves out a file
  its argv names (D1); a `pin` digest does not match or a pinned file is missing; or `argv[0]`, a
  pinned file, the registry, the library, the approval store or the requirement file, or a
  directory holding one, can be written by anyone other than its owner;
- a required attachment names `gemini` or `pi`, or names no engines, on a spawn of one of them (D8);
- the registry or a profile attaches a library entry with only a string `command`, or an Operator
  turn attaches one to a spawn whose baseline holds a required attachment (D1);
- a caller supplied hooks of its own (D4, D5), a settings key outside the version's
  `settings_keys`, a non-empty Claude setting source, or a Claude spawn without
  `--strict-mcp-config`;
- a Codex project layer declares hooks or MCP servers or sets a key outside `project_layer_keys`,
  or the layer set cannot be established;
- under a redaction entry: a prompt-file field (`system_prompt_file`, `append_system_prompt_file`),
  a native resume of a session that did not start under the same redaction baseline, or an MCP
  server that cannot be put behind a proxy (D7);
- under a redaction entry, the run directory's real path or an ancestor of it holds a `.git`
  entry, or one of them cannot be read (D4);
- writing the render or one of D7's run files failed;
- `ndjson_from_cli()` was handed a command whose first element is not `hooks.binary`, or a
  `RenderedHooks` that `compile_agent_hooks` did not build (D3);
- an `agent_prompt` hook failed, timed out or returned a reply that fails D7's check, the
  redaction table it wrote is missing, unreadable, invalid or readable by anyone other than its
  owner, or an output schema holds a value the table holds (D7).

No refusal falls back to a hook-less spawn. A refused fan-out worker fails that worker and the
work that depends on it; workers already admitted finish under their snapshot. A scheduled run or a
review-pipeline job that is refused records a refusal outcome with its cause, as any failed run
does; its next scheduled run or pass is admitted or refused on its own, so a repair takes effect at
the next one and nothing retries in between. The Operator's current hook-less turns are each
replaced by the same refusal: an assembly reference to a hook no longer in the library, skipped
with a log line in `materialize_claude_hooks` (`lionagi/studio/services/hooks_library.py`); an
unusable stored config or library, for which `materialize_settings_file` returns `None`
(`lionagi/studio/services/operator_hooks.py`); and a materializer error of any type, which the
engine's call site catches and answers with no settings (`lionagi/studio/operator/engine.py`). An
assembly that is switched off or empty still renders no Operator attachments, which is the
operator's choice, and the registry's baseline still applies. The fourth hook-less turn, an
execution root that cannot reach the rendered file without traversal (P5), goes away with D4's
absolute render path rather than becoming a refusal. The repair path is the Studio hooks API, which
starts no agent.

What happens after the spawn is the engine's: a hook that crashes or times out is handled by the
engine's own rules. A hook entry that must hold under those failures requires `pre_tool.fail_closed`
(D2), which the conformance suite grants only after injecting a crash, a timeout and malformed
output (A2); an engine version that runs the tool when its pre-tool hook fails does not receive it,
so a spawn of that version with the redaction entry is refused here. A required hook that the
engine silently does not load or does not trust (a Codex trust record that does not match, say) is
not visible before the spawn. The conformance suite grants a version its table only after showing
the render loads and runs (A4), and at run time `agent_exit` finds a tool call that no pre-tool hook
saw and fails the run (D7). For a version whose loading changed without a new version string, that
is detection after the call, not prevention.

Why this way. Each check above is one whose failure would otherwise start an agent with fewer hooks
than its configuration says, which is the silent removal P4 records. Refusing before the spawn costs
a run; a hook-less run of a redaction entry costs the secret, and the codebase already judged a
guard that silently does not run to be worse than none (P2). The repair paths (editing the
registry, the library or the requirement, approving a command, the Studio hooks API) start no agent,
so a refusal never blocks its own repair.

### D7 — The redaction entry's contract

A spawn is under a redaction entry, wherever this record says so, when its compiled attachments for
its engine include one on `agent_prompt`: that event exists for this contract alone (D2). lionagi
keys on it the run files, the proxies, the chosen session id and the refusals D4 and D6 name. The
tool routes and the scans come from the entry's other attachments, which the registry in D1 lists;
an entry that attaches fewer of them gets only what those do.

What "before the engine writes its transcript" means here. Neither engine is known to call a hook
before it persists a tool result. So the redaction entry keeps a value out of the context and the
transcript by substitution before the engine holds the bytes: in lionagi's own hand-off of the
prompt, and on a small set of tool routes the entry mediates. Every other tool is denied, and what
still reaches the engine by another way is looked for afterwards.

What it guards against. The entry keeps secret values out of an agent's context and transcript in
the ordinary course of its work, for an agent that is not trying to defeat it. It is not a boundary
against the agent: the engine's tools, its shell above all, run with the account's authority, and
a process of the same account can read and write every file below, the hook's own files included.
Each place where that matters is named where it arises, and none is claimed as prevented.

The run's files. For a provider call with the redaction entry, lionagi creates three files beside
the render, each by exclusive create with mode 0600, in the run directory, which it creates with
mode 0700, and removes them when the call returns, after `agent_exit`. Every write that replaces a
run file, the hook's included, goes through a temporary file created by exclusive create with mode
0600 in the run directory and renamed over it, so the rename keeps the mode; a temporary file
created under the process's umask would leave the table readable by other accounts. It passes their
paths to every hook process in the environment (`LIONAGI_HOOK_TABLE`, `LIONAGI_HOOK_RECEIPTS`,
`LIONAGI_HOOK_POLICY`), never in argv, so the approved argv is the same for every call. A hook the
engine runs inherits the engine's environment, so the engine's tools see the paths too; the read
policy denies the directory that holds them, and a shell command that prints the table prints it
through the redactor.

- The redaction table: the run's secret set, in two parts. The first is the hook's own configured
  set: at the first `agent_prompt` of a provider call the hook writes it whole into the table, every
  value under its token, whether or not that value occurs anywhere yet, as one JSON object,
  `{"version": 1, "values": {"<token>": "<raw value>", ...}}`, through a temporary file and a rename
  as above. The second is the named part of the set lionagi's log redactor treats as secret in what
  it hands the engine process, as `ndjson_from_cli()` computes it for that redactor
  (`lionagi/providers/_cli_subprocess.py`): `_secret_candidates` takes each variable of the child's
  environment that `secrets.lookup.names` in the user settings declares
  (`fill_declared_secrets_and_names`, `lionagi/providers/_secret_resolution.py`), whatever its
  length, and each whose name matches the redactor's name pattern (`_SECRET_ENV_KEY_RE` in that
  module, an unanchored match on words such as key, token, auth and password) with a value of at
  least eight characters; `_cmd_secret_values` takes, at the same length, the value of an assignment
  argument whose name matches the pattern, and the argument after any argument whose text matches
  it, unless that one starts with `-`. The log redactor also removes values this part leaves out,
  and they are not in the table: a matching name's value shorter than eight characters, which the
  log replaces only as a whole token and the table, which matches text inside text, would find
  inside ordinary words; and every other value of eight characters or more, whatever its name. It is
  a rule about names, not a reading of every value. A credential in a variable no one declared and
  whose name matches none of those words (a connection string in `DATABASE_URL`, say), or one
  shorter than eight characters under a matching name no one declared (`DB_PASSWORD=hunter2`), is
  not in the set; a deployment that hands an agent one declares its name or puts it in the hook's
  configured set. And a matching name brings its value in whatever it holds, credential or not:
  `SSH_AUTH_SOCK`'s socket path, a path in `KEYBOARD_LAYOUT_DIR`, the text of an `AUTHOR_NAME`, the
  word `password` in `AUTH_METHOD`. Each such value is then handled as a credential everywhere
  below: a prompt that holds it is refused, a read or a shell command returns it as its token, a
  tool result that holds it is blocked, and a transcript that holds it fails the run. With
  `AUTH_METHOD=password` in the engine's environment, a task whose text says "password" is refused.
  A deployment that runs agents under the entry can leave such variables out of the environment it
  hands them (the request's `env`). That cost is accepted: it is the rule lionagi's logs already
  apply to named values, and a whole-word match, the obvious narrowing, would miss a name such as
  `OPENAI_APIKEY`. Before each child's spawn, the first turn's and the continuation's, lionagi adds
  each value of the second part the table lacks, under a token it mints from the variable's name,
  through the same temporary file and rename, and then runs the prompt check (`agent_prompt`, below)
  again over the texts that spawn carries, so a value of either part in a prompt refuses the spawn.
  No hook and no lionagi step writes the table while a child runs, except lionagi's restore before
  that child's `agent_exit` (below); every event and every check reads it. So every check below
  compares against the whole set, not only values a hook has replaced, and two hook implementations
  that configure different sets are told apart by their tables. A credential in neither part, such
  as one in a file the account holds or in an undeclared variable whose name does not match, is not
  in the set, and nothing in this record treats it as secret. A table that is missing, unreadable,
  invalid or readable by anyone other than its owner after `agent_prompt` refuses the spawn (D6);
  one found so later fails the run. The table holds raw values: it is the one run file A1 excludes,
  and it never leaves `LIONAGI_HOME/run/hooks`.
- The read policy: the directories and deny rules the run's own permissions apply to reads, written
  by lionagi from the final request, with `LIONAGI_HOME/run/hooks` always denied. A read the hook
  performs for the model obeys it, so while the file holds the bytes lionagi wrote, the hook never
  reads more than the run's permissions let the engine read; an edit to it opens the window named
  below. A shell command stays under the engine's own permission rules and sandbox, as
  without the entry; the read policy does not govern it.
- The receipts: one JSON line per hook invocation, appended by the hook, naming the event, the
  engine's id for the tool call, and the route taken or the denial; a stop scan appends its count.
  An engine version whose pre-tool hook input carries no call id is not granted `all_tools_gated`
  (D2), so every receipt a check matches has an id. The receipts detect an engine that ran a tool
  without its hook; they do not detect an agent that appends to them through its shell.

Edits during a run. Before each child's spawn lionagi keeps the bytes of the table, of the read
policy and of the Claude render file as it last wrote them. Before it runs that child's
`agent_exit` hook it compares each file with those bytes; a difference fails the run, and lionagi
writes its own bytes back first, so the exit scan reads the whole set. The comparison sees only a
difference that is still there at the exit: a shell command that edits one of these files and
restores its bytes before the child exits passes it. Between an edit and its restore, or the exit,
an emptied or edited table blinds the redactor, the post-tool check and the stop scan, and what is
printed then reaches the model; an edited read policy lets a hook-performed read return a file the
run's permissions deny; and an edited render can drop a hook if the engine reads its settings again
during the run, which no measurement here has shown either way.

The events:

- `agent_prompt`: fired by lionagi, not by the engine, in the provider's `_ndjson_from_cli` on the
  final request, before the spawn, for every spawn including the `auto_finish` continuation. lionagi
  runs the entry's argv with `{"version": 1, "run_id": ..., "engine": ..., "texts": [{"channel":
  ..., "text": ...}, ...]}` on stdin and reads one JSON object, `{"texts": [...]}`, from stdout,
  with the same channels in the same order. The channels are every text lionagi hands the engine as
  model input: the prompt, the system prompt and its appendix, the prompt and description of each
  `agents` entry, and the earlier turns lionagi replays when it resumes no engine thread. A
  prompt-file field is refused instead (D6), because the engine reads that file itself. The output
  schema (Claude's `--json-schema` text, the file Codex's `--output-schema` names, which lionagi
  reads) is model input too; lionagi checks it against the whole table, in the check it runs again
  before the spawn, and never rewrites it, because a rewritten schema changes the output's contract,
  so a value of the table in it refuses the spawn. The check: the hook exited 0 within its timeout,
  the reply has that shape, and no returned text contains a raw value the table holds. Otherwise the
  spawn is refused; when the check passes, the spawn carries the returned texts.
- `pre_tool` (requires `pre_tool.deny`, `pre_tool.rewrite`, `pre_tool.fail_closed`,
  `all_tools_gated`): the entry admits only a call whose output is mediated before the engine
  receives it, on one of three routes.
  - A read the hook performs. For a file-read tool the hook reads the file itself, where the read
    policy admits it, and returns the redacted text as the deny reason the model sees. A read the
    policy refuses is denied.
  - A shell command whose output passes the redactor. The hook rewrites the command so its standard
    output and standard error pass through the redactor. The rewritten input never carries a raw
    value: where a command needs a secret, the redactor's process supplies it, and the engine sees
    only the token. The route mediates what the command prints and nothing else it does, because the
    command runs with the account's authority. Outside the route: its file effects (a command that
    copies a secret into a file leaves it there, and a later read of that file is mediated like any
    read); a read of a file the read policy denies, which the engine's own permissions govern;
    another engine it starts (`claude -p`, `codex exec`), which runs outside any render and writes
    its own transcript, while its printed output still comes back through the redactor; and a write
    to the run's receipts, read policy or table, which the exit comparison finds for the table and
    the read policy when the change is still there at the exit (above). The redactor finds a value
    only as its exact text, so a command that prints one encoded, transformed or split across writes
    (`base64`, a hex dump, a value cut in two) passes the route, the table file included, whose path
    the command's environment holds. A3 records each of these; none is refused.
  - An MCP call through a redacting proxy. For the run, lionagi replaces the complete MCP server set
    the engine sees with proxies bound to that run and to the upstream configuration the request
    carried, and the hook admits an MCP call only when its server is one of them. Each proxy redacts
    every message it forwards to the engine with the run's table: discovery metadata and schemas,
    results, structured results and errors. A message form it does not support, or output that
    cannot be redacted and still satisfy its schema, becomes a sanitized failure. A proxy failure
    never connects the engine to the upstream server directly and never repeats a call that may
    have completed. The proxy keeps each server's authorization as it was; redaction grants no
    permission.

  Every other tool is denied, native search, edit and write tools and nested agents included, until
  the conformance suite shows a route for it at that engine version.
- `post_tool` (requires `post_tool.block`): checks the result the model is about to see and blocks
  it if a raw value is present. This protects the model's view when substitution missed; it does
  not undo what the engine already wrote.
- `claude:Stop` / `codex:Stop`: scans the session's transcript for every value the table holds and
  appends the count to the receipts. It counts and does not rewrite, because the engine is still
  running and may append to the file. `agent_exit` reads the count, and a count above zero fails
  the run there.
- `agent_exit`: fired by lionagi, not by the engine, from `ndjson_from_cli()` after each child exits
  for any reason (success, error, signal, timeout, cancellation). Its unit is the child process, so
  a provider call with an `auto_finish` continuation fires it twice, and either failure fails the
  provider call. lionagi runs the entry's argv with `{"version": 1, "run_id": ..., "engine": ...,
  "child": <n>, "exit": {"code": ...} | {"signal": ...}, "session_id": ... | null, "transcript":
  <absolute path> | null}` on stdin. The hook scans the transcript for every value the table holds
  and scrubs what it finds, which no longer races the engine's appends, and checks the receipts: a
  tool call in the transcript whose id has no pre-tool receipt fails the run, because a hook the
  engine did not run saw nothing. A value the scan found fails the run even after it is scrubbed,
  because the transcript held it, and so does a stop count above zero. Where the transcript is: the
  engine names it to its own hooks (Claude's hook input carries `transcript_path`), and the first
  hook that runs records it in the receipts. When no hook ran, lionagi derives it from the session
  id and the working directory by the layout the conformance suite recorded for that engine
  version (A4); a version with no recorded layout has no table (D2). For Claude, lionagi chose the
  session id before the spawn (D4), so the transcript can be found whenever the child started. For
  Codex the id comes from the stream; when none arrived and no hook recorded a path, the payload
  carries no transcript, the hook scans nothing, and the run fails with its transcript
  unidentified. The hook runs under the library entry's `timeout`; a timeout, a crash or a
  non-zero exit is a failure and fails the run. A cancelled run waits for the scan, shielded from
  the cancellation, up to that bound, and then reports the run failed with the scan unfinished.

Native resume. Under the redaction entry lionagi resumes an engine session only when its own run
record shows that the session started under the same redaction baseline (the same redaction
attachments, by digest). Any other resume is refused (D6), because the engine would load history
that no hook saw. The `auto_finish` continuation resumes its own first turn's session and meets
this.

A parent that dies. If lionagi's own process dies before a child exits, `agent_exit` does not fire.
The render, table and receipts files left under `LIONAGI_HOME/run/hooks` then mark that call's
transcript as unscanned, and the table still holds raw values. This record does not recover them
(Consequences).

The guarantee. Prompt redaction and the mediated routes are prevention, for the texts and calls they
cover. `all_tools_gated` is the capability that says the pre-tool hook sees every call, and the
conformance suite is the only thing that grants it, per engine version. The post-tool, stop and exit
steps are detection and cleanup. A value that reaches the engine by no mediated route (an
instruction file the engine loads on its own, say) is not prevented, and the stop and exit scans
find it only when the engine writes it into its transcript, which A1 measures per engine version;
the post-tool hook sees tool results only. lionagi's own stores
(its stored messages, the stream log) then hold it as the engine emitted it, and no hook scrubs
them. Attaching the redaction entry restricts an agent's tools to the routes above and puts its MCP
access behind the proxy.

Why this way. lionagi's prompt hand-off, the pre-tool hook and the MCP proxy are the only points at
which the engine has not yet held the bytes, so substitution there is the only mechanism that keeps
a value out of the context and the transcript rather than removing it afterwards. The prompt is
lionagi's own text, so lionagi redacts it itself and needs no engine hook for it. The proxy exists
because an agent needs its MCP servers and the engine connects to them itself: only a transport the
engine actually connects to can mediate what it returns, and a server name or a tool-name prefix is
not proof of routing. Denial covers everything else because a call the entry cannot mediate must
fail closed. The stop scan counts and the exit hook scrubs because a rewrite is safe only after the
engine has exited. The table and the receipts are files because a hook runs as a new process for
each event, and lionagi needs the table to check `agent_prompt`'s reply and the receipts to find a
call the engine ran without its hook.

### D8 — Scope

- In scope: every process lionagi spawns through `ndjson_from_cli()` whose binary is `claude` or
  `codex`, by every path in P3, including the Operator, the `auto_finish` continuation and resumed
  turns. Processes that do not pass that function are outside this record (Context, Out of scope).
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

- **A1 — redaction end to end, on the mediated paths.** A fixture of secret values, given in each
  prompt channel D7 names and read through each route (a file read, a shell command, a read-only MCP
  call) by a Claude agent and a Codex agent that lionagi spawned, comes back as tokens. Zero raw
  values in: what the model saw, the engine's native transcript or rollout, lionagi's own stored
  messages, the stream log, stderr, and the run's artifacts. Checked during the run, not only after
  `Stop`. A fixture value in the output schema refuses the spawn, on each engine. Excluded: the
  fixture's input files, the redaction table, and lionagi's record of the caller's instruction as
  the caller sent it. Two fixtures are recorded and not required clean, because D7 names them as
  outside prevention: a shell command that copies a fixture file to a new workspace file (the copy
  holds raw values, and a later read of it comes back as tokens), and a fixture value in an
  instruction file the engine loads on its own (the run records whether the engine wrote the value
  into its transcript and whether the stop and exit scans found it; lionagi's stores hold it).
  Each run file is mode 0600 after every write, the table's write through its rename included, and
  the run directory is mode 0700.
- **A2 — failure injection.** An `agent_prompt` hook that fails, times out, replies in another shape
  or returns a raw value; pre-tool hook timeout, crash and malformed output (the evidence for
  `pre_tool.fail_closed`); an exit hook that times out; the engine killed mid-run and before its
  first event; lionagi's own process killed mid-run (the leftover files D7 names); a resumed run,
  and a native resume of a session started without the entry; a Claude `auto_finish` continuation
  (two exit events, one run outcome, and an argv that carries the first turn's id as `--resume` and
  neither `--session-id` nor `--continue`); two concurrent runs; a registry edited during a run; a
  missing hook command, a relative `argv[0]`, an `agent_prompt`, pre-tool or exit entry with no
  `timeout` and one whose `timeout` is a boolean, and an unsupported event; a Codex trust record
  that does not match; an Operator turn whose assembly attaches an argv entry, one that attaches a
  legacy string entry, one whose assembly references a hook no longer in the library, one whose
  stored config or library is unusable, and one whose materializer raises. The legacy string entry
  runs, with its deprecation notice, when the baseline holds no required attachment, and is refused
  when it holds one; each other case either refuses before the spawn or leaves zero raw values on
  the mediated paths; the exit-hook timeout and the unmatched trust record fail the run.
- **A3 — every tool path, each one useful.** A file read inside the read policy returns its text
  with tokens, and one outside it is denied. A shell command writing to stdout and stderr, and
  incremental input to a running command. A read-only MCP call returns a tokenized secret with its
  other fields intact, and an authorized mutating call runs exactly once, also when the proxy fails
  after the upstream call. Fixture values placed separately in MCP discovery metadata, a structured
  result, an error, a notification and split transport chunks come back as tokens or as a sanitized
  failure. With the proxy killed, a direct endpoint under the same server name and a caller-supplied
  server override reach the engine with nothing. Native search, edit and write tools and a nested
  agent are denied. A shell command that prints the agent's environment returns each value of the
  table's second part as a token. Seven shell-route cases are recorded and not required refused,
  because D7 places them outside the route: another engine started by a shell command (its printed
  output comes back as tokens; its own transcript is outside the scans), a shell read of a file the
  read policy denies, a shell command that appends to the run's receipts, a shell command that
  prints a fixture value base64-encoded, a fixture credential printed raw from an undeclared
  `DATABASE_URL` and from an undeclared `DB_PASSWORD` shorter than eight characters, a shell command
  that empties the table and then prints a fixture value, which reaches the model while the run
  fails at exit, and a shell command that widens the read policy, after which a hook-performed read
  returns a file the run denies, recorded once with the edit left in place (the run fails at exit)
  and once with the bytes restored before the exit (the run passes). With `AUTH_METHOD=password` in
  the engine's environment, a prompt that holds the word is refused and a file read returns it as
  its token (D7). A representative MCP-dependent task runs to completion, so a blanket denial cannot
  pass. What the model saw and what was persisted are checked separately.
- **A4 — isolation canaries.** A hook in the user's Claude settings, a hook in a repository's
  `.claude/settings.json`, a stdio server in a repository's `.mcp.json` under a spawn that resolves
  none (`li agent --no-mcp-config`, and every other P3 path), a Codex user `hooks.json`, a Codex
  project layer with hooks, a symlinked project root and an organization-managed setting. The first
  six never run (the Codex project layer refuses the spawn); the seventh is recorded. The other half
  of the `.mcp.json` case: under `li agent` without either flag, the same server is in the server
  set lionagi passes, and under a redaction entry it is reached through its proxy (D4). At each
  version the suite also shows that the render loads and runs (a pre-tool receipt for the first
  tool call), records the transcript path the engine names to its hooks and checks that the
  derivation from session id and working directory gives the same path (D7), and the implementing
  PR records whether `CLAUDE.md` is still discovered with setting sources empty (D4).
- **A5 — every path renders.** One agent started by each path in P3 (`li agent` new and resumed, flow,
  fan-out worker and synthesis, play, each MCP submit verb, an agent schedule, the review pipeline,
  the Operator, an engine agent and an orchestration-pattern agent built by `create_agent`, and a
  Claude `auto_finish` continuation) shows the rendered flags in its own argv, read from the running
  process, and the rendered file's digest matches its snapshot. A test lists the callers of
  `ndjson_from_cli()` under `lionagi/` and fails when one is added that is not a CLI provider's
  `_ndjson_from_cli`; the test suite's direct calls pass a render the compiler built (D3). A
  wrapper command (`env claude ...`) and a `RenderedHooks` built by hand are each refused.
- **A6 — the deployment requirement.** The same launcher runs against two homes. With no
  requirement and no registry, both launches succeed under isolation, create no registry, and
  record the resolved path and its absence. With a requirement naming the first home's registry and
  its redaction attachments, the first launch succeeds; changing the home, deleting the registry,
  replacing it with an empty one, or weakening a mandatory attachment each refuses before the
  engine starts. An invalid present registry refuses in both modes. Every A5 path carries the
  requirement. Each of the four starters D1 names (the MCP job runner, the scheduler, the run-resume
  worker and the MCP verb dispatcher) starts a child from an environment filtered to leave
  `LIONAGI_HOOKS_REQUIREMENT` out, and the child still reads the requirement, because each sets it
  after building that environment (D1). A `li` command run by an agent's shell reads the same
  requirement, because `ndjson_from_cli()` sets it in the engine's environment, and one that
  removes it from its own environment runs without it; the test records both (D8).
- **A7 — command approval.** The same command attached by hand in the registry, through the Studio
  library API, through a profile and as an argv entry through the Operator refuses without the
  approval of its final argv and runs after it; the Operator's legacy string entries, which need
  none until they are translated (D1), are A2's. A pasted plugin command with no provenance field is
  treated the same. Changing the arguments or the `{engine}` substitution needs a new approval;
  moving an unchanged entry between files neither grants nor revokes one. An approval in a project's
  settings file and a precomputed Codex trust record authorize nothing. A missing or unreadable
  approval store refuses, and a changed pinned file under an unchanged argv is refused by the pin,
  separately from the approval. `li hooks trust` lists every concrete argv before enrollment.
- **A8 — what the tables list.** Each key in `settings_keys` and `project_layer_keys`, set by a
  caller or by a project layer, is shown by a fixture at that engine version to run no command,
  widen no permission and set no environment value, credential or endpoint; each event in
  `inert_events` is shown to neither rewrite a tool's input nor add to the model's context. A key
  or event without that fixture stays out of the table, and a later layer that adds a rewriting
  pre-tool hook to a spawn with a required baseline is refused.
- **A9 — each remaining refusal.** One fixture per D6 refusal that A2 to A8 do not reach: an
  attachment naming an entry the library lacks; a registry and a library, each unreadable and each
  invalid; a library entry with no `argv`, and one whose `argv[0]` is not executable; the file
  `LIONAGI_HOOKS_REQUIREMENT` names missing, unreadable and invalid; an approved `argv[0]` replaced
  in place (a new content digest) and one whose path resolves to another executable; an attachment
  naming an event its target does not render, and one requiring a capability its target's table
  lacks; a pinned file deleted; a registry, library, approval store, requirement file, pinned file
  or `argv[0]`, or a directory holding one, writable by someone other than its owner; a required
  entry with no `pin`, and one whose `pin` leaves out a file its argv names; a required attachment
  that names `gemini` or `pi`, and one that names no engines, each on a `gemini` and a `pi` spawn; a
  registry or profile attaching a string-command entry; caller-supplied hooks, a settings key
  outside `settings_keys`, a non-empty Claude setting source, and a Claude spawn built without
  `--strict-mcp-config`; a Codex project layer that declares MCP servers, one that sets a key
  outside `project_layer_keys`, and a layer set that cannot be established; under a redaction entry,
  each prompt-file field and an MCP server that cannot be put behind a proxy; under a redaction
  entry, a `LIONAGI_HOME` inside the agent's work tree, one under a home directory that is itself a
  repository, with the agent started outside every repository so that only the run directory's
  ancestors can refuse it, a `LIONAGI_HOME` that is a symbolic link into a repository, and a run
  directory with an ancestor the check cannot read; a failed write of the render or of a run file;
  and a redaction table missing, unreadable, invalid or readable by another account after
  `agent_prompt`. Each raises `AgentHooksRefused` and starts no engine process. One fixture runs the
  Operator from its default root (the home directory, not itself a repository), and that turn
  renders and spawns; with a home that is a repository, a spawn without a redaction entry renders
  and spawns.

## Consequences

- Every Claude and Codex agent lionagi starts runs the hooks declared in the registry and its
  layers, plus whatever the engine's managed policy adds, and a deployment that declares a
  requirement runs its baseline or nothing. Changing the redaction implementation is
  a library edit and a new approval.
- Spawned agents stop seeing the user's interactive engine configuration. Anything they relied on
  from it (permissions, plugins, MCP servers, model defaults) has to be passed by lionagi
  explicitly, and some will be found missing after the change. That is the intended direction;
  the cost lands once.
- Agents become refusable for configuration reasons. A broken registry stops every spawn on the
  machine until it is repaired, the Operator included; an absent one does not, unless the
  deployment declared a requirement. A hook guard that silently stops guarding is the failure this
  trades away. A home directory that is itself a repository, with `LIONAGI_HOME` at its default
  under it, or a `LIONAGI_HOME` that is a repository or links into one, refuses every spawn under a
  redaction entry until `LIONAGI_HOME` resolves outside every work tree (D4); spawns without the
  entry are not affected.
- Every command attached to a spawned agent needs an operator's approval of its final argv, the
  Operator's legacy string entries excepted until they are translated (D1), so enrolling the
  baseline, and any change to an attachment's arguments, is an operator step. An
  approval is not immutability: a pin is checked when a spawn is compiled, and a process of the
  same account can change the files between the compile and an event.
- Attaching the redaction entry restricts an agent to the mediated routes. Native search, edit and
  write tools and nested agents are denied until a route is shown for them, and every MCP server
  lionagi passes is reached through a per-call proxy, which costs availability and turns unsupported message forms
  into failures.
- What the redaction entry does not prevent, named so that nothing else claims it: what a shell
  command does with the account's authority beyond its printed output (its file effects, a read the
  read policy denies, another engine it starts, a write to the run's receipts, read policy, table or
  render, of which the exit comparison finds the last three after the call, and only when the change
  is still there at the exit); a value the engine takes in by itself (instruction files), which the
  scans find only when the engine writes it into its transcript and lionagi's stores keep; MCP
  servers and connectors the engine loads from its own managed or system configuration, which
  lionagi does not pass and so does not proxy; the Operator's legacy string entries, which run as
  shell text until they are translated, on spawns whose baseline holds no required attachment; a
  value printed encoded, transformed or split, which the redactor does not recognize; a credential
  in neither part of the table (an undeclared variable whose name does not match, a value shorter
  than eight characters under a matching name no one declared, a file the account holds); run files
  inside a work tree that D4's rule does not see (one a repository elsewhere names, or another
  version control system's); a lionagi process that dies, which leaves its call's transcript
  unscanned and its table, holding raw values, on disk; an engine version whose hook loading changed
  without a new version string, found after the call by the receipts check; lionagi's record of the
  caller's instruction as sent, which holds the raw prompt; and an agent steered by text it reads,
  such as a review agent following an instruction planted in a pull request, which is an agent
  trying to defeat the entry: an instruction to print a value encoded passes the redactor.
- An engine upgrade stops the agents that require a capability until the conformance suite runs
  against the new version. Pinning engine versions becomes part of operating lionagi.
- `ndjson_from_cli()` gains a required argument and refuses a command its render was not compiled
  for; every caller changes once, the four providers and the test suite's 52 direct calls.
- Reversal: D1-D3 are cheap to reverse (the registry can hold zero attachments). D4 and D5's
  isolation is the expensive part to reverse, because once agents run without the user layer,
  configurations that only worked through it are gone from use.

## Alternatives considered

### Refuse every spawn while the registry is absent

It would catch a home that resolved to the wrong place. It also refuses the first agent of every
installation that never declared hooks, which an upgrade must not do. Creating an empty registry on
first use is worse: it makes a lost registry look intended. Absence alone cannot tell a new
installation from a wrong home, so D1 takes that knowledge from a requirement the launcher names
outside `LIONAGI_HOME`, and a requirement file under `LIONAGI_HOME` would vanish with the home it
is meant to check.

### Deny MCP calls under the redaction entry, or scrub them afterwards

Denying every MCP call is honest and simple, and a protected agent that needs one server query then
fails its task while A3's leakage check still passes. Letting the calls run and scrubbing the
transcript later keeps MCP usable, and the model has already seen the value by then. D7 puts MCP
behind a redacting proxy and keeps denial for every tool that has no route.

### Give the registry and library a trust tier of their own

An owner-only file under `LIONAGI_HOME` could be trusted as user-authored. A plugin command pasted
into it would then run with no approval, and ADR-0048 D7 gives no tier by location. D1 asks for an
approval of each final argv instead.

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

- Measured at lionagi `main` 208bcc08: no spawn path other than the Operator sets `setting_sources`;
  `CodexCodeRequest` has no user-config isolation field; the library maps `post_response` to `Stop`;
  the library validator accepts `description`, `command` and `timeout` only, with `timeout` optional
  and unchecked for sign. The change in #3671 (open) changes the first two: flow and fan-out workers
  run Claude with empty setting sources and Codex with `--ignore-user-config`, through the field
  that change adds. This record builds on that change and merges after it.
- Engine behaviors this record relies on (Claude's `--setting-sources`, `--strict-mcp-config` and
  `--session-id`, Codex's `--ignore-user-config`, project layers, hook trust records, hook decision
  shapes) are taken from each vendor's documentation and are not yet measured at a pinned version.
  D2's capability tables exist so that nothing depends on them until the conformance suite has
  measured them.
