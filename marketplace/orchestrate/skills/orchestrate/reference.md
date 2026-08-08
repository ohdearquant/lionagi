# Orchestration reference

Complete parameter reference for lionagi orchestration: the MCP tool (primary) and the
`li` CLI (secondary, checkout-local).

---

## MCP: the `request` tool

The plugin ships one MCP server, keyed `lion`, so the tool a Claude session actually sees is:

```
mcp__plugin_orchestrate_lion__request
```

A plugin-provided server's tools are always namespaced
`mcp__plugin_<plugin-name>_<server-name>__<tool>` — never the bare `mcp__<server-name>__<tool>`
form a server name alone might suggest. This plugin is named `orchestrate`, the server key is
`lion`, so the scoped name above is the only one that resolves to a real tool.

The tool takes exactly two parameters:

- `ops` — an **array of objects**, each `{"op": "<verb>", "args": {...}}`. Not a DSL string.
  Multiple ops in one call run in order; a batch is capped at 8 ops.
- `help` — `true` for the full verb catalog, `"<verb>"` for that verb's parameter schema, or
  `{"verb": "<verb>", "playbook": "<name>"}` to resolve a playbook's declared arguments.
  **`help` and `ops` cannot be combined in one call** — a catalog and a list of op results are
  different shapes, so ask for help in its own call first.

Every call returns `{"status": "success"|"partial", "ops": [...]}`, one entry per op in the
order given, each `{"ok": true, "op", "result"}` or `{"ok": false, "op", "error"}`. A failing
op never fails the whole call — check each op's own `ok`.

```
mcp__plugin_orchestrate_lion__request(help=true)
```

Read the catalog before writing a call you're not sure of. It names every verb's required
parameters and either the `schema_fingerprint` to send or a marker that it varies with the
named playbook (see below) — often enough to write the call correctly with no second
round-trip.

## The verbs these skills use

The catalog is larger than this table, and it is the authority: read it with `help=true`
rather than treating any list written here as closed. Two things it tells you that a table
cannot. First, it names verbs beyond orchestration — schedules, run history, lifecycle and
state reads, team and invocation reads, server health. Second, some entries are there to
say the server will *not* run them, each with its reason: `team.send` and `invoke.start`
are named that way, so a call to one is refused with an explanation rather than failing as
an unknown verb.

The verbs the skills in this bundle actually call:

| Verb | Does | Detached run? | Needs `schema_fingerprint`? |
|---|---|---|---|
| `agent.submit` | Run one agent on one task. | yes | yes |
| `flow.submit` | Plan and run a DAG of agents with dependencies. | yes | yes |
| `fanout.submit` | Run N agents on one task in parallel, optionally synthesized. | yes | yes |
| `play.submit` | Load a saved prompt and defaults, then plan and run a flow. | yes | yes |
| `job.status` | Current state of a run: liveness, job record. | no | no |
| `job.output` | Console tail and artifact list of a run. | no | no |
| `job.list` | Recent runs, newest first, optionally filtered by status. | no | no |
| `job.wait` | Observe one or more runs until terminal or the time window closes. | no | no |
| `job.kill` | Stop a running job by its run id. | no | no |
| `profile.list` | Agent profiles `agent.submit` would accept here. | no | no |
| `profile.show` | What one profile name resolves to. | no | no |

What the server offers is the **released** lionagi's catalog: `uvx --from lionagi[mcp] li mcp
serve` resolves the latest release, not any local checkout's `main`. A verb you found by
reading a checkout may therefore not be there yet, and `help=true` is what settles it.

Every `*.submit` verb spawns a **detached** background run and returns a run id immediately;
there is no blocking "wait for the final answer" call. Follow up with `job.status`,
`job.wait`, or `job.output` using the returned id.

## The `schema_fingerprint` step

`agent.submit`, `flow.submit`, `fanout.submit` and `play.submit` each require a
`schema_fingerprint` as a **sibling of `args`**, not a member of it:

```
{"op": "flow.submit", "args": {"query": ["claude"], "prompt": "..."}, "schema_fingerprint": "<from help>"}
```

For `agent.submit` and `fanout.submit`, take it from `help=true` (their catalog entry carries
it directly) or from `help="<verb>"`.

`flow.submit` and `play.submit` are different, and getting this wrong is the one mistake here
that costs a round-trip for a reason that is not obvious. Their schema depends on which
`playbook` the call names, because the playbook's own declared arguments are resolved into it.
So the fingerprint depends on it too. An argument-free flow has its own fingerprint, while
`play.submit` intentionally withholds one until a playbook is named. Two cases to write for:

- **No `playbook` in `args`** — `help="flow.submit"` is the right source. Only `flow.submit`
  can be called this way; `play.submit` requires a playbook.
- **A `playbook` in `args`** — the fingerprint must come from
  `help={"verb": "<verb>", "playbook": "<the same name>"}`. Unqualified
  `help="play.submit"` returns the verb's schema but intentionally omits a fingerprint,
  because no successful `play.submit` call can use the argument-free schema. The catalog marks
  both playbook-aware verbs with `schema_fingerprint_varies_with: ["playbook"]`.

Omitting the fingerprint, or sending one from the wrong schema, is refused with the current
fingerprint **for the schema your call actually resolved** and with the whole op spelled out.
For a playbook-aware call, the refusal's remedy repeats both the verb and playbook in its
qualified `help` object. Use that value directly or re-fetch with the exact qualified help call.

## Calling each verb

### `agent.submit` — one agent, one task

```
{"op": "agent.submit", "args": {"query": ["claude"], "prompt": "Write unit tests for auth.py"}, "schema_fingerprint": "<from help>"}
```

`query` carries the CLI's positionals: an optional model followed by a prompt. When `prompt`
is supplied separately as above, put only the model in `query`. Other arguments mirror CLI
flags with underscores in place of dashes, including `agent`, `resume`, `continue_last`,
`effort`, `cwd`, `timeout`, and `project`; there is no separate `model` key. Ask
`help="agent.submit"` for the exact set this build admits, and call `profile.list` before using
a named `agent` profile.

### `flow.submit` — DAG orchestration

```
{"op": "flow.submit", "args": {"query": ["claude"], "prompt": "Audit auth, implement fixes, verify with tests", "with_synthesis": true, "dry_run": true}, "schema_fingerprint": "<from help>"}
```

Runs a `dry_run` first to preview the planned DAG without executing it, then resend without
`dry_run` (with a fresh fingerprint if the args that vary it changed) to commit. There is no
separate "foreground" mode — every `flow.submit` call is already a detached background run;
read it back with `job.status` / `job.wait` / `job.output`.

### `fanout.submit` — parallel workers

```
{"op": "fanout.submit", "args": {"query": ["claude"], "prompt": "Review this codebase for security issues", "num_workers": 4, "with_synthesis": true}, "schema_fingerprint": "<from help>"}
```

### `play.submit` — a saved playbook

```
{"op": "play.submit", "args": {"playbook": "feature", "prompt": "Add JWT middleware"}, "schema_fingerprint": "<from help={\"verb\": \"play.submit\", \"playbook\": \"feature\"}>"}
```

`playbook` is required. Its stored prompt, defaults, and declared arguments feed the same
planner and executor used by `flow.submit`.

### `job.status` / `job.output` / `job.kill` — one run

```
{"op": "job.status", "args": {"run_id": "20260730T091500-a1b2c3"}}
{"op": "job.output", "args": {"run_id": "20260730T091500-a1b2c3", "tail_chars": 20000}}
{"op": "job.kill", "args": {"run_id": "20260730T091500-a1b2c3"}}
```

`job.output`'s `tail_chars` counts from the *end* of the console log; raise it when a run's
final answer is longer than the default tail. The artifact list comes back in full regardless.

### `job.list` — recent runs

```
{"op": "job.list", "args": {"limit": 10, "status": "running"}}
```

### `job.wait` — observe until terminal

```
{"op": "job.wait", "args": {"run_ids": ["20260730T091500-a1b2c3"], "max_wait": 60, "poll_interval": 1}}
```

Takes an *array* of run ids and returns one entry per id, in that order. `max_wait` is
clamped to 0-600 seconds; a window closing before every run is terminal is not an error —
the result carries every observation made so far, and calling again is safe.

### `profile.list` / `profile.show` — what agents exist here

```
{"op": "profile.list", "args": {}}
{"op": "profile.show", "args": {"name": "<name returned by profile.list>"}}
```

`profile.show` needs `name`; an unknown name is refused with the full list of names that do
exist, rather than an empty result.

---

## `li` CLI (secondary, checkout-local)

Available only inside a lionagi checkout with `li` on `PATH` — not through the plugin's MCP
server, and not the path the worked examples above use. Kept here because a handful of things
are genuinely easier from a terminal: opening a completed run's `--show-graph` artifact, or
scripting `li invoke start`/`li invoke end` and `li team` sessions, neither of
which the MCP surface exposes today (see `teams-and-tracking.md`).

### `li agent [MODEL] PROMPT` — single agent

```
li agent claude "Write unit tests for auth.py"
li agent claude/opus-4-6-high "Produce a security audit"
li agent -r <branch-id> "Follow-up question"
li agent -c "Continue the previous conversation"
```

| Flag | Default | Description |
|---|---|---|
| `MODEL` | (positional, optional) | Provider/model spec, e.g. `claude`, `codex`, `claude/opus-4-6-high` |
| `PROMPT` | (positional, required) | Task text |
| `-a / --agent NAME` | — | Load agent profile from `.lionagi/agents/<NAME>.md` |
| `-r / --resume BRANCH_ID` | — | Resume a previous branch by ID |
| `-c / --continue-last` | false | Continue the most recently used branch |
| `--yolo` | false | Auto-approve all tool calls |
| `--bypass` | false | Bypass all codex approvals and sandbox |
| `--effort LEVEL` | — | `low\|medium\|high\|xhigh\|max` (claude); `none\|minimal\|low\|medium\|high\|xhigh\|max\|ultra` (codex, where `max`/`ultra` clamp per model support) |
| `--cwd DIR` | — | Working directory for CLI provider |
| `--timeout SECONDS` | — | Kill after N seconds |
| `--invocation ID` | — | Parent invocation id (from `li invoke start`) |
| `--project NAME` | — | Explicit project name; overrides auto-detection |
| `-v / --verbose` | false | Stream real-time output |
| `--theme light\|dark` | — | Terminal display theme |
| `--fast` | false | Codex priority service tier |

Exit codes: `0` completed, `1` failed, `124` timed out, `130` aborted (Ctrl-C), `143` cancelled.

### `li o fanout [MODEL] PROMPT` — parallel workers

```
li o fanout claude "Review this codebase for security issues" -n 4
li o fanout claude/sonnet "Suggest API design approaches" -n 3 \
    --with-synthesis claude/opus-4-6-high
```

| Flag | Default | Description |
|---|---|---|
| `MODEL` | (positional, optional) | Orchestrator model; also default worker model |
| `PROMPT` | (positional, required) | Task for the orchestrator to decompose |
| `-a / --agent NAME` | — | Load orchestrator profile |
| `-n / --num-workers N` | 3 | Max assignments the orchestrator generates; caps `--workers` specs beyond it |
| `--workers M1,M2,...` | — | Explicit comma-separated worker model specs |
| `--max-concurrent N` | 0 (all) | Max workers running at once |
| `--with-synthesis [MODEL]` | false | Enable synthesis. Bare flag uses orchestrator model |
| `--synthesis-prompt TEXT` | — | Custom synthesis instruction |
| `--save DIR` | — | Save all outputs to directory |
| `--team-mode [NAME]` | — | Create a team for inter-worker messaging |
| `--output text\|json` | text | Output format |
| `--yolo` | false | Auto-approve tool calls for all workers |
| `--bypass` | false | Bypass approvals for all workers |
| `--effort LEVEL` | — | Effort level for all workers |
| `--cwd DIR` | — | Working directory |
| `--timeout SECONDS` | — | Kill after N seconds |
| `--invocation ID` | — | Parent invocation id |
| `--project NAME` | — | Explicit project name |

### `li o flow [MODEL] [PROMPT]` — DAG orchestration

```
li o flow claude "Audit and harden the authentication module" \
    --with-synthesis --save ./audit-out --yolo --bypass
li o flow -f ./my-spec.yaml --yolo --bypass
li o flow -p feature "Add JWT middleware" --save ./out --yolo --bypass
```

| Flag | Default | Description |
|---|---|---|
| `MODEL` | (positional, optional) | Orchestrator model spec |
| `PROMPT` | (positional, optional) | Task; can come from spec file's `prompt:` |
| `-f / --file PATH` | — | Load flow spec from YAML/JSON. CLI flags override |
| `-p / --playbook NAME` | — | Load from `~/.lionagi/playbooks/<NAME>.playbook.yaml` |
| `-a / --agent NAME` | — | Load orchestrator profile |
| `--with-synthesis [MODEL]` | false | Final synthesis after all ops complete |
| `--max-concurrent N` | 0 (all) | Max agents running in parallel within a phase |
| `--save DIR` | — | Save outputs (required with `--background`) |
| `--team-mode [NAME]` | — | Fresh team per invocation |
| `--team-attach NAME` | — | Attach to existing team (mutually exclusive with `--team-mode`) |
| `--dry-run` | false | Plan DAG without executing |
| `--show-graph` | false | Write the executed DAG visualization after the run finishes |
| `--background` | false | Fork into background subprocess (requires `--save`) |
| `--bare` | false | Ignore agent profiles; all workers use CLI model |
| `--max-ops N` | 0 (unlimited) | Cap total DAG nodes. `--max-agents` is deprecated alias |
| `--output text\|json` | text | Output format |

Plus all common flags (`--yolo`, `--bypass`, `--effort`, `--cwd`, `--timeout`, `--invocation`, `--project`).

### `li play NAME [PROMPT] [ARGS...]` — playbook sugar

```
li play feature "Add JWT middleware"
li play list                     # list available playbooks
li play feature --help           # show playbook description and args
```

All `li o flow` flags work with `li play` (except `-p`).

### `li team` — persistent team messaging

```bash
li team create "my-team" -m "researcher,writer,reviewer"
li team list
li team show my-team
li team send "Found a critical bug" --team my-team --to all --from analyst
li team receive --team my-team --as reviewer
```

### `li invoke` — invocation tracking

```bash
INV=$(li invoke start --skill orchestrate --prompt "Full audit")
li o flow claude "..." --invocation "$INV" --yolo --bypass
li invoke end "$INV" --status completed
li invoke list --skill orchestrate --limit 10
```

Statuses: `completed`, `failed`, `timed_out`, `aborted`, `cancelled`.
