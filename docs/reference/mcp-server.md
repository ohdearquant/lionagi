# MCP Server Reference

The operations LionAGI exposes to an [MCP](https://modelcontextprotocol.io)
client, and the one tool they are reached through.

## Purpose and transport

`li mcp` (equivalently `python -m lionagi.mcp`) starts an MCP server that speaks
over **stdio**. There is no HTTP listener and no port to configure: the client
launches the process and talks to it on its standard streams.

The server is a control plane over the `li` CLI, not a second implementation of
it. Spawn and job verbs run `li` as a detached subprocess and keep a job record
beside it; the remaining verbs run `li <path> --machine` and return the versioned
envelope that command emits. A verb exists here only because it was registered,
so adding a command to the CLI does not widen this surface.

It advertises **one tool**, `request`. Every operation is a namespaced verb
passed to that tool rather than a tool of its own, because an advertised tool
schema is sent to the model on every request in every session for as long as the
server is registered. A verb's parameters are fetched by asking for them.

## Install and client registration

The server needs the optional `mcp` extra:

```bash
pip install 'lionagi[mcp]'
```

Importing `lionagi.mcp` does not pull that dependency. Only serving does, so a
missing extra surfaces when you run `li mcp`, with a message naming the install
command.

Register it with any MCP client, for example in an `.mcp.json`:

```json
{
  "mcpServers": {
    "lion": { "command": "li", "args": ["mcp"] }
  }
}
```

The key in `mcpServers` is your client's local name for the entry, and it is
what your client addresses the server by. The name the server reports over the
protocol is `lion`. Earlier builds reported `lionagi`, which is the name older
registrations and logs show.

## The `request` tool

`request` takes two optional inputs:

| Input | Type | Meaning |
|-------|------|---------|
| `ops` | list of objects | Operations to run, each `{"op": "<verb>", "args": {...}}`. A spawn verb's op also carries `"schema_fingerprint"`. At most 8 ops per call. |
| `help` | `true`, a verb name, or `{"verb": ..., "playbook": ...}` | Ask what exists instead of running anything. |

Passing neither is an error that says so. Exceeding the op limit is an error
naming the count, never a truncation that would run part of a batch.

**Result contract.** Ops run **in order**, and a failing op does not stop the
ops beside it. The reply is:

```json
{
  "status": "success",
  "ops": [
    {"ok": true, "op": "job.status", "result": {"...": "..."}}
  ]
}
```

`status` is `"success"` when every op succeeded and `"partial"` when any op did
not. A per-op failure never fails the call, so **the caller must check each
`ok`**. A failed entry carries `error` instead of `result`, with a `kind` and a
`message`, and a rejected op also carries the schema it was judged against, so a
wrong parameter tells you the right shape in the same reply.

Argument validation is closed: an unknown or misspelled parameter is refused by
name rather than ignored. Every value comes back as raw machine JSON, with no
relative timestamps, formatted durations, or tables.

### Asking what exists

`help=true` returns the catalog. Trimmed to its envelope, the reply looks like:

```json
{
  "verbs": [
    {
      "verb": "agent.submit",
      "available": true,
      "summary": "Run one agent on one task as a detached background run.",
      "required": []
    }
  ],
  "verb_count": 68,
  "available_count": 40,
  "max_ops": 8,
  "help_usage": "help=true returns this catalog; help='<verb>' returns that verb's full parameter schema; ...",
  "synonyms_removed_after": "2026-09-30"
}
```

`help='<verb>'` returns that one verb's full parameter schema:

```json
{
  "verb": "job.status",
  "schema": {
    "type": "object",
    "properties": {
      "run_id": {
        "type": "string",
        "description": "Id of a background run as returned by a submit verb (format YYYYMMDDTHHMMSS-<6hex>). An id with no job record answers with known=false rather than failing."
      }
    },
    "additionalProperties": false,
    "required": ["run_id"],
    "title": "job.status",
    "description": "Current state of a background run: liveness, job record, CLI manifest."
  }
}
```

A spawn verb's help also returns a `schema_fingerprint`, which that verb's ops
must carry. `help={"verb": "<verb>", "playbook": "<name>"}` additionally
resolves that playbook's own declared arguments into the schema.

## The catalog

40 verbs are reachable. Both tables in this section are checked against the
registry by the test suite, so a verb added or withdrawn without updating them
fails a test rather than going unnoticed. The registry is the authority; ask it
directly with:

```bash
python -c "from lionagi.mcp import verbs as v; print(len(v.VERBS)); [print(n) for n in sorted(v.VERBS)]"
```

<!-- mcp-catalog:available:start -->

| Verb | Summary |
|------|---------|
| `agent.submit` | Run one agent on one task as a detached background run. |
| `dispatch.ack` | Acknowledge a delivered dispatch with its ack token, so the queue stops redelivering it. A wrong token is refused without echoing the real one. |
| `dispatch.ls` | Rows in the durable dispatch outbox, newest first, without their payloads. |
| `dispatch.purge` | Delete one dispatch row by id, whatever its status, recording an audit row. Deleting by --status/--before is refused here: a sweep deletes rows the caller never named and reports a count for rows that can no longer be inspected. |
| `dispatch.retry` | Return a failed or dead-lettered dispatch to pending so delivery is attempted again. |
| `dispatch.show` | One dispatch row in full, including its payload and ack token. |
| `doctor` | Environment checks and which of them failed. |
| `fanout.submit` | Run N agents on one task in parallel, optionally synthesized. |
| `flow.submit` | Plan and run a DAG of agents with dependencies, in the background. |
| `handshake` | The machine-result contract version this build speaks. |
| `invoke.list` | Recent skill-level invocations, newest first. |
| `job.kill` | Stop a background job by signalling the process group this server created. |
| `job.list` | Recent background jobs, newest first, optionally filtered by status. |
| `job.output` | Console tail and artifact list of a background run. |
| `job.status` | Current state of a background run: liveness, job record, CLI manifest. |
| `job.wait` | Observe runs until terminal or the window closes; partial results, never a bool. |
| `lifecycle` | What the lifecycle store records about one run: whether every session it opened has ended, and with what outcome. |
| `monitor` | Entities in flight right now: sessions, invocations, shows, plays. |
| `play.submit` | Run a saved playbook: a flow whose plan and prompt are already written down. |
| `plugin.info` | One plugin's version, trust state, and everything its manifest declares. |
| `profile.list` | Agent profiles agent.submit would accept here, each with the file it comes from and the configuration it resolves to. |
| `profile.show` | What one agent profile name resolves to: its winning file, the files it shadows, and its effective configuration. |
| `runs` | Recorded runs on disk and what each one wrote. |
| `schedule.create` | Write a schedule row, and report when its trigger next resolves in the scheduler's own timezone. |
| `schedule.delete` | Remove a schedule row. Reports the deletion the store confirmed. |
| `schedule.disable` | Stop a schedule firing. Reports the state that was committed. |
| `schedule.enable` | Let a schedule fire again. Reports the state that was committed. |
| `schedule.export` | Convert schedule rows into ScheduleSet documents, returned inline. |
| `schedule.get` | One schedule in full, including its ten most recent runs. |
| `schedule.limits` | The global concurrent-fire cap and how many fires are in flight now. |
| `schedule.list` | Every schedule this Studio holds, with its trigger and enabled state. |
| `schedule.runs` | Runs of one schedule, newest first, optionally filtered by status. |
| `schedule.status` | Did it work: the schedule header, its latest run, and that run's verdict. |
| `schedule.trigger` | Fire a schedule now: reports the run id allocated, never that the run ran. |
| `schedule.validate` | Whether a ScheduleSet file resolves, and what each schedule resolves to. |
| `server.info` | Which build is serving: version, contract version, uptime, verb counts. |
| `state.ls` | Sessions in the lifecycle store with their branch and message counts. |
| `state.stats` | Store and write-ahead-log size, per-table row counts, session status spread. |
| `stats.runs` | Run counts and first/last timestamps, grouped by project/kind/agent/model/status. |
| `team.list` | Teams on disk with their members and message counts. |

<!-- mcp-catalog:available:end -->

There is deliberately **no parameter table here**. A verb's parameters are
projected from the CLI parser at call time, so a table written by hand would
drift away from what the server actually accepts. Ask for the parameters
instead, with `help='<verb>'`, and you get the schema the call will be validated
against rather than a copy of it.

## Operations the surface does not offer

Thirty further names are catalogued as **unavailable**, each with its
reason. They are not omissions: a caller that asks what exists gets the name and
why it cannot be called, which is a different answer from the name never having
been considered. The right-hand column below abbreviates the reason; `help=true`
returns each one in full.

<!-- mcp-catalog:unavailable:start -->

| Verb | Summary | Not offered because |
|------|---------|---------------------|
| `casts` | The built-in roles and modes an agent can be composed from. | no machine result |
| `orchestrate.ctl.status` | What a running flow's control plane reports about it. | no machine result |
| `state.doctor` | Read-only inspection of the lifecycle store. | no machine result |
| `team.create` | Messaging between agents working as a team. | no machine result |
| `team.receive` | Messaging between agents working as a team. | no machine result |
| `team.send` | Messaging between agents working as a team. | no machine result |
| `team.show` | Messaging between agents working as a team. | no machine result |
| `state.checkpoint` | Writes against the lifecycle store. | invalidating write |
| `state.import` | Writes against the lifecycle store. | invalidating write |
| `state.import-teams` | Writes against the lifecycle store. | invalidating write |
| `state.prune` | Writes against the lifecycle store. | invalidating write |
| `state.vacuum` | Writes against the lifecycle store. | invalidating write |
| `state.null-content` | Reclaiming the space held by old message bodies. | irreversible loss |
| `hooks.import` | Importing hook commands from another tool's config. | grants privilege |
| `plugin.disable` | Plugin bundle enablement. | grants privilege |
| `plugin.enable` | Plugin bundle enablement. | grants privilege |
| `plugin.list` | Installed plugin bundles and their trust state. | grants privilege |
| `orchestrate.ctl.msg` | The running-flow control plane. | effect lands elsewhere |
| `orchestrate.ctl.pause` | The running-flow control plane. | effect lands elsewhere |
| `orchestrate.ctl.resume` | The running-flow control plane. | effect lands elsewhere |
| `orchestrate.ctl.resolve` | Close a control whose consumer claimed it and never reported back. | a human's finding, not a report |
| `invoke.end` | Opening and closing a skill-level orchestration record. | no caller identity |
| `invoke.start` | Opening and closing a skill-level orchestration record. | no caller identity |
| `mirror` | Mirror Claude Code sessions into Studio, live. | a process, not a call |
| `studio.start` | The Studio server. | a process, not a call |
| `engine.run` | Run a domain-specific multi-agent pipeline. | spawns without a job record |
| `kill` | Terminate a run, session, play or show by id. | no identity to correlate |
| `mcp` | Serve this surface over stdio. | it is this server |
| `schedule.apply` | Reconcile a whole ScheduleSet file into the store, atomically. | shape undecided |
| `schedule.run` | One schedule run. | already covered |

<!-- mcp-catalog:unavailable:end -->

The largest group is *no machine result*: the CLI path emits no versioned
machine result (`li <path> --machine`), so there is nothing to return that is
not scraped console text. Scraping would make a command's console wording an API
contract, so the fix belongs in the CLI, where the command gains a machine-result
seam. The others are refusals on their own terms rather than gaps waiting to be
filled — a write whose effect no machine result can describe, a path that would
grant the calling agent a right it did not have, a control-plane signal whose
effect lands on a running flow rather than in a reply, or a long-lived process
that never returns at all.

Asking for one of these inside `ops` returns a catalogued answer rather than a
bare unknown-verb error: the op comes back `ok=false` with an `unavailable`
error carrying that verb's reason and summary. A name that was never registered
at all is a `not_found` error instead, pointing you at `help=true`.

Separately, a small set of CLI paths that grant privilege to the caller has no
verb at all, and no verb accepts opaque argv, so there is no route to them
through this surface.

## Worked example

Submit an agent, then observe it. First ask for the schema, because a spawn
verb's op must carry the fingerprint that help returns. The fingerprint below is
illustrative: it tracks the verb's current schema, so always send the one your
own help call returned rather than a value copied from here.

```json
{"help": "agent.submit"}
```

```json
{
  "verb": "agent.submit",
  "schema": {"type": "object", "properties": {"prompt": {"...": "..."}}, "...": "..."},
  "schema_fingerprint": "947259f8208faddc"
}
```

Then submit:

```json
{
  "ops": [
    {
      "op": "agent.submit",
      "args": {"prompt": "Summarise the changes on this branch.", "agent": "reviewer"},
      "schema_fingerprint": "947259f8208faddc"
    }
  ]
}
```

The reply carries the allocated `run_id`. Poll it, or read what it wrote:

```json
{"ops": [{"op": "job.status", "args": {"run_id": "<run_id>"}}]}
```

```json
{"ops": [{"op": "job.output", "args": {"run_id": "<run_id>"}}]}
```

To block instead of polling, use `job.wait`, which observes runs until they are
terminal or the window closes and returns partial results rather than a bare
boolean. Because ops run in order in one call, a status read and an output read
can travel together:

```json
{
  "ops": [
    {"op": "job.status", "args": {"run_id": "<run_id>"}},
    {"op": "job.output", "args": {"run_id": "<run_id>"}}
  ]
}
```

Check each entry's `ok`: the second can fail while the first succeeds, and
`status` would then be `"partial"`.

## Terminal state of a background run

`job.status`, `job.list` and `job.wait` all resolve through one classification
path, so no two of them can disagree about the same run. Branch on `terminal`
and `outcome`; `status` is an open producer vocabulary and is for display.

`outcome` is the closed set `succeeded | failed | cancelled | indeterminate`.
**`indeterminate`** paired with `reason_code: "process_gone_without_outcome"` is
how a run ends when an observation positively established that its process is gone — the recorded
pid held no process, it disappeared between two probes, or a live process holds
the number and started at a different time — and no authoritative outcome was
ever reported for it. Nothing survived to write one, so the run is ended here
rather than left waiting forever.

That end is not a failure. The work may have had its intended effect before the
process died. Consumers must not map it to `failed`, to success, or to
cancellation, and must not retry it automatically: an unreported external side
effect may already have committed.

Three fields describe such an end:

| Field | Meaning |
|-------|---------|
| `terminal_source` | what wrote the end: `cli_terminal_hook`, `lifecycle_cache`, `spawn_failure`, `mcp_kill`, or `mcp_orphan_reaper`. Null on records written before the field existed. Carried by both `job.status` and `job.list`. |
| `terminal_evidence` | bounded evidence behind an end nobody reported: the kind, and the named finding. Never argv, environment, logs or payloads. |
| `liveness_conclusion` | what the observation established: `process_gone`, `alive` or `unknown`. Only `process_gone` can end a run. |

An `unknown` conclusion — a pid the OS cannot be asked about, an identity probe
that was denied or unreadable — never ends a run. Those stay non-terminal and
advisory (`possibly_orphaned`), and `job.wait` reports them under
`stopped_without_end`.

**`finished_at` on a run ended this way is when the end was established and recorded,
not when the process exited**, which nothing surviving can report. Any duration
derived from it is an upper bound on how long the run actually ran.

`job.wait`'s **`all_terminal` means every valid requested run has a recorded
end**, including `indeterminate` ones. It does not mean every run succeeded or reported
an outcome — read each entry's `outcome` for that.

## Compatibility

An earlier version of this server advertised one tool per operation. Those flat
names are still accepted as **synonyms** inside `ops` and resolve silently to
their namespaced verb:

| Old name | Verb |
|----------|------|
| `submit_agent` | `agent.submit` |
| `submit_flow` | `flow.submit` |
| `submit_fanout` | `fanout.submit` |
| `submit_play` | `play.submit` |
| `job_status` | `job.status` |
| `job_output` | `job.output` |
| `job_kill` | `job.kill` |
| `job_wait` | `job.wait` |
| `jobs_list` | `job.list` |
| `server_info` | `server.info` |

They exist for callers already scripted against them, not as something new
callers should learn, which is why they do not appear in the catalog. They will
be **removed after the date the catalog reports as `synonyms_removed_after`**,
currently `2026-09-30`. Write new calls against the namespaced verbs.

If your client shows a single entry in `tools/list`, that is correct and
current. The operations are behind `request`, and `help=true` lists them.
