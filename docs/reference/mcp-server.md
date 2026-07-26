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
  "verb_count": 47,
  "available_count": 36,
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

36 verbs are reachable. This list is generated from the registry:

```bash
python -c "from lionagi.mcp import verbs as v; print(len(v.VERBS)); [print(n) for n in sorted(v.VERBS)]"
```

| Verb | Summary |
|------|---------|
| `agent.submit` | Run one agent on one task as a detached background run. |
| `dispatch.ls` | Rows in the durable dispatch outbox, newest first, without their payloads. |
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

There is deliberately **no parameter table here**. A verb's parameters are
projected from the CLI parser at call time, so a table written by hand would
drift away from what the server actually accepts. Ask for the parameters
instead, with `help='<verb>'`, and you get the schema the call will be validated
against rather than a copy of it.

## Operations the surface does not offer

Eleven further names are catalogued as **unavailable**, each with its reason.
They are not omissions: a caller that asks what exists gets the name and why it
cannot be called, which is a different answer from the name never having been
considered.

| Verb | Summary |
|------|---------|
| `schedule.apply` | Reconcile a whole ScheduleSet file into the store, atomically. |
| `schedule.run` | One schedule run. |
| `team.create`, `team.show`, `team.send`, `team.receive` | Messaging between agents working as a team. |
| `state.doctor` | Read-only inspection of the lifecycle store. |
| `dispatch.ack`, `dispatch.retry`, `dispatch.purge` | The outbound dispatch queue. |
| `plugin.list` | Installed plugin bundles and their trust state. |

Most share one reason: the CLI path emits no versioned machine result
(`li <path> --machine`), so there is nothing to return that is not scraped
console text. Scraping would make a command's console wording an API contract,
so the fix belongs in the CLI, where the command gains a machine-result seam.
The rest carry their own reason: `schedule.run` is already covered by
`schedule.runs`, `schedule.apply` has no decided machine-result shape yet, and
`plugin.list` prunes trust records as part of listing, which is a write.

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
