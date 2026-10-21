# ADR-0133: Machine-wide admission for CLI agents

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: implemented — D1-D7
- **Area**: service-providers
- **Date**: 2026-10-18
- **Relations**: extends ADR-0030 D4 (one subprocess entry point for agentic providers); read with
  ADR-0029 (per-model request admission, a different layer) and ADR-0106 D8 (exit statuses)

## Context

A CLI agent is a process tree, not a request. `codex`, `claude`, `agy` and `pi` each start a
runtime, and most start their own MCP servers and tool subprocesses beneath it. A machine that
runs agents from several places at once (a fan-out of reviewers, a flow's workers, scheduled
legs, someone's own `li agent`) starts as many of these trees as the callers ask for, and nothing
in lionagi counts them across processes.

**P1 — no cap spans processes.** Every limit lionagi had on concurrent work lived inside one
process: a flow's `max_concurrent`, a fan-out's worker count, an `iModel`'s rate-limited processor
(ADR-0029). Ten `li` processes each within their own limit can start ten times that many agents,
and the machine pays for all of them. When memory runs out, the kernel does not refuse the next
agent; it slows or kills whatever is running, including work that was nearly done.

**P2 — every engine spawns through one function, and nothing sits there.** Since ADR-0030 D4,
every agentic CLI provider starts its child through `ndjson_from_cli()` in
`lionagi/providers/_cli_subprocess.py`. It is the one point every CLI agent passes, whatever
started it, and the right place for a count; until now it admitted every call.

**P3 — "read-only" agents cost the same and were treated the same.** A reviewer that only reads
and a builder that compiles both hold a full agent tree, but they do not need the same budget:
the expensive part of a builder is what it runs (compilers, test suites), and a reader that is
confined to its working directory cannot start those on the rest of the machine. Without a class
there was no way to give readers more room than builders, and no enforced meaning to "this agent
only reads".

**P4 — a refusal had no shape.** A caller that wanted to hold an agent back had nothing to raise
that a launcher could tell from the agent's own failure, so a capped spawn would read as a failed
run.

**P5 — nothing bounded how deep agents start agents.** A CLI agent can run `li agent` itself,
through a shell tool or through an MCP server it was given, and the agent that starts can do the
same. Each level holds its slots while it waits on the next, so under a cap a nested agent waits
for a slot its own ancestor holds until it is refused, and without one nothing stops the chain.

| Concern | Decision |
|---------|----------|
| Where the count happens | D1: one admission call in `ndjson_from_cli()` before the child starts |
| What is counted, and against what | D2: pools `all`, `heavy` and one per engine, capped by environment variables; unset or `0` means no cap |
| Who holds a slot, and when it frees | D3: an `flock` held by the spawning process on a per-slot file, plus a record naming the child |
| What happens at the cap | D4: wait up to 600 s by default, then refuse with `SubagentAdmissionRefusedError`; `li agent` exits 75 |
| What `light` means per engine | D5: confinement the engine can enforce, or a refusal where it can enforce none |
| How a person sees it | D6: `li monitor` prints held slots per pool; machine mode carries the same data |
| Agents inside agents | D7: an agent lionagi started cannot start another through lionagi; refused at once, exit 1 |

Out of scope:

- **API-model agents.** An API model leg is a request, not a process tree; its concurrency is the
  request-admission policy of ADR-0029. Admission here applies only where a CLI child starts.
- **Memory or CPU measurement.** A cap is a count. Whether the host has room is a different
  question with a different instrument, and admission does not read the host's memory.
- **Scheduling order between waiting agents.** Waiters retry independently; there is no queue
  and no priority (Alternatives).
- **Network filesystems.** Slot files live under `LIONAGI_HOME`; `flock` on a network filesystem is
  not a cross-host lock, and a cap is per machine.

## Decision

### D1 — One admission call at the single spawn site

`ndjson_from_cli()` takes a lease before it starts the child and holds it until the child's
process group has ended:

```python
async def ndjson_from_cli(
    cmd: list[str],
    *,
    ...,
    engine: str | None = None,
    workload: str = DEFAULT_WORKLOAD,   # "heavy"
) -> AsyncIterator[dict]: ...

# lionagi/providers/_subagent_admission.py
async def admit_subagent(
    *,
    engine: str | None,
    workload: str = DEFAULT_WORKLOAD,
    environ: Mapping[str, str] | None = None,
) -> SubagentLease: ...

@dataclass
class SubagentLease:
    slots: list[_Slot]
    def record_child(self, pid: int, create_time: float | None) -> None: ...
    def release(self) -> None: ...
```

Each provider passes its engine name: `codex`, `claude`, `gemini`, `pi`.

**Exact semantics.**

- With no pool capped, `admit_subagent()` returns an empty lease and touches no file. An
  uncapped install behaves as it did before this ADR.
- The nested-agent check (D7) runs before admission, and the stdin payload is encoded before it,
  so neither a nested agent nor an input that cannot be encoded waits for or holds a slot.
- The lease is taken before `create_subprocess_exec`, so a refused agent has no child.
- After the spawn, `record_child(pid, create_time)` writes the child's identity into every slot.
- The lease is released when the spawn raises, when the `on_spawn` observer raises, and in the
  generator's final `finally` after `end_child_group()`, so a slot frees only once the group has
  been ended.
- A cancellation that lands while the spawn is in flight releases the slot as it unwinds; the
  abandoned-spawn callback (ADR-0030 D4) still ends a child that arrives afterwards, so that child
  can outlive its slot by the time the creation call takes to return.

**Why this way.** The spawn site is the only point every CLI agent passes: `li agent`, flow and
fan-out workers, schedules and direct library use all reach it, and none of them has to opt in. A
cap placed in any one launcher covers only that launcher's agents (P1).

### D2 — Pools, caps and their variables

| Pool | Counts | Variable |
|------|--------|----------|
| `all` | every CLI agent | `LIONAGI_MAX_SUBAGENTS` |
| `heavy` | agents whose workload is `heavy` | `LIONAGI_MAX_SUBAGENTS_HEAVY` |
| `<engine>` | one engine's agents | `LIONAGI_MAX_SUBAGENTS_<ENGINE>`, e.g. `LIONAGI_MAX_SUBAGENTS_CODEX` |

**Exact semantics.**

- Variables are read from the admitting process's environment at every admission, so a change
  applies to the next agent without a restart. A pool stays full at the smallest cap its holders
  read (D3), so a raised cap admits past the agents admitted under the lower one only as they
  release; a lowered cap applies at once.
- Unset, empty or `0` means the pool has no cap.
- A value that does not parse as a whole number (`thirty`, `-1`, `2.5`) refuses the agent. A
  misspelled cap must not let agents start uncounted on a machine whose owner believes it is
  capped.
- An agent takes a slot in each capped pool it counts against, or in none (D3). It locks the
  pools in one order, sorted by pool name, so two admissions never each hold a lock the other is
  waiting for. A `light` agent never touches `heavy`.

**Why this way.** The total cap is the machine's budget. The heavy cap is narrower because heavy
agents are the ones whose own work (builds, test runs) competes for the machine. An engine cap
exists because engines differ: one engine's account may allow fewer concurrent sessions than the
machine does.

### D3 — A slot is a lock held by the spawning process, with the child named in it

A slot is the file `$LIONAGI_HOME/leases/subagents/<pool>/<index>.lock`, `index` in
`0..cap-1`, held with `flock(LOCK_EX | LOCK_NB)`. The directory is created `0700` and the file
`0600`. The holder writes a record into the file:

```json
{"cap": 4, "child_pid": 48211, "child_started": 1792345678.21, "engine": "codex",
 "holder_pid": 48190, "since": 1792345677.95, "workload": "light"}
```

`child_pid` and `child_started` are `null` until `record_child()` runs.

**Exact semantics.**

- **The lock belongs to the lionagi process, never to the child.** The descriptor is opened
  `O_CLOEXEC`, so the child and anything it spawns inherit nothing. A grandchild that daemonizes
  cannot keep a slot, and the kernel drops the lock when the holder exits, however it exits.
- **A recorded child that is still running keeps its slot.** The holder can die while its
  child, which leads its own session, keeps running. A free lock whose record names a child that
  is still the same process (pid and start time, through `lionagi.ln._proc`) counts as held. An
  unreadable process table counts as running, so a slot is never handed out on a probe that
  could not look. A record with no start time for its child (the spawn could not read one, or
  the child does not lead its own process group) does not count: its pid alone could name a
  process that took the pid over, so once the holder exits that slot reads free even while the
  child still runs, and a pool can then hold one agent past its cap.
- **Counting and taking happen under the pool's admission lock.** Each pool has an admission lock,
  `<pool>/.admission`. An admission takes the admission locks of all its pools before it counts
  any, counts every held slot at every index on disk, and takes a slot in each pool only once all
  of them have room; a slot taken before a later pool turns out full goes back. It is admitted to
  every pool or to none, never reads another admission's half-taken slots, and holds no slot in
  one pool while it waits on another. The admission locks are released before any wait.
- **A pool is full at the smallest cap in play.** Each record carries the cap its holder read. Two
  processes can read different caps from their environments, so a pool is full when its held
  slots reach the smallest of the admission's own cap and every cap its holders recorded: a
  process reading a cap of 3 cannot run a pool past a holder that read 2.
- **Release clears the record before unlocking**, so a released slot never names a child.
- **A lock with no record is a holder between those two steps.** It counts as held, and a pool
  that is full only because of one is re-read within milliseconds, up to 25 times, before any
  wait; a slot counted free and found locked when taken is treated the same. Without this an
  admission with a wait of 0 could be refused for a slot that was freeing that instant.
- **A record that cannot be read is no record.** A file over 64 KiB, nested past the JSON
  parser's depth, holding anything but a JSON object, or failing to read reads as no record, as
  the empty file a release leaves does. A free slot with such a record is free even when the
  child it names still runs: the process-table rule above is about a probe that could not look,
  and holding the slot on a record nobody can read would keep it taken until the file was
  deleted by hand. A locked slot with one still counts, as a lock with no record, but no cap is
  read from it, so that holder's cap no longer lowers the pool's limit.
- A pool directory that cannot be created or written refuses the agent.
- On a platform without `flock` (Windows) an uncapped agent runs as before and a configured cap
  refuses, because nothing could hold the slot it would count.

**Why this way.** A per-slot `flock` gets crash release from the kernel: no reaper, no stale pid
to recycle, no daemon that has to be alive for a cap to hold. The record is what makes a slot
outlive its holder when the child survives it, which a bare lock cannot do.

### D4 — Waiting, refusal and its exit status

When a pool is full the admission retries every two seconds, jittered by ±25%, until
`LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS` (default `600`; `0` refuses at once) runs out, then raises:

```python
class SubagentAdmissionRefusedError(ProviderError):   # lionagi/providers/_provider_errors.py
    retryable: ClassVar[bool] = True
    pool: str
    cap: int
    holders: tuple[int | None, ...]
```

with one line of text, for example:

```text
subagent admission refused: heavy codex agent waited 600s; pool 'heavy' is at its cap of 6
(LIONAGI_MAX_SUBAGENTS_HEAVY), held by pid 48190 (child 48211), 48302 (child 48317), ...
```

**Exact semantics.**

- A wait that does not parse as a non-negative finite number refuses.
- The refusal is a `ProviderError` with `retryable = True`: the same request admits once a slot
  frees, or once the cap rises and the holders that read the lower cap have released (D3).
- `iModel.stream()` re-raises a stream failure as a `ValueError` caused by the provider's error.
  `li agent` walks that cause chain: a refusal anywhere in it prints the refusal itself, not the
  wrapper, and the command exits **75** (`EX_TEMPFAIL`, `EXIT_CODE_SUBAGENT_REFUSED` in
  `lionagi/cli/_util.py`).
- The run row records the failure as a retryable provider failure naming
  `SubagentAdmissionRefusedError` (`lionagi/cli/_runs.py` already walks the cause chain for its
  reason), and the MCP terminal cause records the same class, through the same walk.

**Why 75 when a run row exists.** ADR-0106 D8 keeps 78 for "nothing executed and no run exists".
A refused agent does have a run row, recorded as failed, because admission happens inside the
turn. The exit status answers a different question: whether running the same command again is
the remedy. A launcher that requeues on 75 and reports on 1 needs no log parsing to do the right
thing, and the row it would otherwise read says the same.

**Why 600 seconds and 2-second polls.** Ten minutes rides out an agent finishing on a busy machine
and still surfaces a stuck pool to the caller well before a long run's own timeout. Without a
broker there is no cross-process wake-up, so a waiter polls; two seconds bounds how long a freed
slot sits idle, and the jitter keeps two waiters that just collided from retrying in step.

### D5 — Workload classes and what `light` confines

`workload` is `light` or `heavy` (default). It reaches the provider as a runtime-only field on
every CLI request model (`exclude=True`, never serialized):

| Engine | `light` |
|--------|---------|
| codex | `--sandbox workspace-write` (or `read-only` when asked), `-c sandbox_workspace_write.writable_roots=[]`, `-c sandbox_workspace_write.network_access=false`, no linked-worktree `--add-dir`, and the account's apps and plugins turned off as `contain_account_integrations` turns them off. `account_connectors`, `bypass_approvals`, `sandbox='danger-full-access'`, `add_dir` and any `config_overrides` key under `sandbox_mode` or `sandbox_workspace_write` are refused with `CLIInputRefusedError`, at construction and again where the process would start. The sandbox keeps the temporary directories writable, and codex starts the MCP servers it is given outside it |
| claude_code | `Edit`, `Write` and `NotebookEdit` are added to `--disallowedTools`, merged with the caller's list into one flag. Bash stays. Claude Code has no filesystem sandbox, so this is a tool-list boundary only: a command run through Bash can still write |
| gemini-cli | `--sandbox`, always |
| pi | refused before the process starts: Pi has no sandbox lionagi can pin |

A `heavy` request's command line is exactly the one it had before workloads existed.

The class is chosen as follows:

- `li agent --workload light|heavy` wins, then the profile's `workload:` frontmatter, then
  `heavy`. A profile value other than the two classes reads as `heavy` with a warning.
- A resumed conversation runs with this launch's class, not the one stored on the branch:
  resuming a `light` leg without `-a` or `--workload light` runs it `heavy`.
- An orchestration worker takes its own profile's class; a bare worker (no profile) is `heavy`.
  The orchestrator takes its profile's class, which confines the orchestrator's own process and
  not the plan it writes: the flow process the caller started runs each worker with that
  worker's class, so a `light` orchestrator can plan `heavy` workers from its roster. This is
  deliberate. The roster is the caller's grant, the orchestrator cannot start an agent itself
  (D7), and a caller who wants only `light` workers hands a roster of `light` profiles.
- A node spawned during the run takes its class from a worker. A request naming an assignee is
  cloned from that worker's branch and keeps its class, as a planned worker would, so a `light`
  worker can name a `heavy` one from the roster just as the orchestrator can plan one. A request
  naming none takes the class of the worker that emitted it: a dependent one is cloned from that
  worker's branch, an independent one from the orchestrator's branch and then given the emitting
  worker's class, so asking for an unnamed agent does not lift a `light` worker's confinement.
  That clone keeps the orchestrator's other settings, so one a `light` agent cannot have refuses
  the spawn before its process starts. When the orchestrator runs on another engine than the
  `light` worker that asked (two spellings of one provider, such as `claude` and `claude_code`,
  are one engine), an API model included, the independent request is refused with
  `CLIInputRefusedError` before any node exists, since `light` confines an agent differently on
  each engine and not at all on an API model; the worker names an assignee, or asks for a
  dependent agent, which runs on its own engine. A request no worker emitted (the orchestrator's
  own, or one with no emitter) runs `heavy`, as a bare worker does: the orchestrator's class
  confines only its own process. An escalation retry is cloned from the branch of the node it
  retries and keeps that node's class.
- A spawned node that a resume runs again (`--retry-failed`) is rebuilt from the checkpoint
  without that setup: with an assignee it runs on that worker's branch and class, without one on
  the orchestrator's branch or a clone of it, with the orchestrator's class. The checkpoint
  records the class of a `light` worker that asked for a node naming no assignee, and
  `--retry-failed` refuses with `FlowResumeError` to run such a node again. A resume without the
  flag refuses every failed node, so a run in which such a node failed is re-run from scratch. A
  checkpoint written before that record existed carries no class, and its nodes run again as
  described.
- A `light` codex worker is handed no `add_dir`, so its one writable root is its working
  directory, the worker's artifact directory; the project-root `add_dir` every other CLI worker
  is handed is unchanged. A codex worker in team mode needs the team directory writable, so a `light` codex
  worker there is refused with `CLIInputRefusedError` before it starts.
- API-model legs take no workload; the field is passed only to CLI providers, and only when it is
  not the default.
- `agent.submit` on the MCP surface exposes the flag with the same two values. A `light`
  profile's resolved configuration carries `workload: light`; a heavy one carries no key, and
  `profile.list` asked for `resolved.workload` answers `null` for it.

**Why refuse instead of drop.** A setting that would lift the sandbox is refused, not silently
removed: a caller who asked for `light` and `bypass_approvals` has two incompatible intentions, and
the refusal tells them to declare the agent `heavy`. Quietly keeping one would run an agent under a
policy nobody chose.

**Why claude keeps Bash.** A reader needs to search and run read-only commands, and Claude Code's
search tools do not cover everything a reviewer runs. Removing Bash would make `light` unusable for
the agents it is for; keeping it means `light` on Claude bounds the tool list, and this ADR says so
rather than calling it a sandbox.

### D6 — What a person sees

```python
def subagent_admission_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]
# {"root": ".../leases/subagents",
#  "caps": {"LIONAGI_MAX_SUBAGENTS": 30, "LIONAGI_MAX_SUBAGENTS_HEAVY": 6, ...},
#  "pools": {"all": {"cap": 30, "held": 3, "holders": [{"holder_pid": ..., "child_pid": ...,
#            "engine": ..., "workload": ..., "since": ...}]}, ...}}
```

`li monitor` prints one line under its table, `subagents: all 3/30, heavy 1/6, codex 2/4`, and
`li monitor --machine` carries the dict under `subagents`.

**Exact semantics.**

- The status read writes no slot. It counts each pool under the pool's admission lock (D3), as an
  admission does, so the two never see each other's half-done work.
- Caps shown are the reading process's own. A shell without the variables shows held counts with
  no cap.
- No cap and no held slot prints no line, so an uncapped machine's table is unchanged.

### D7 — An agent lionagi started cannot start another through lionagi

```python
# lionagi/providers/_subagent_admission.py
def refuse_nested_agent(engine: str | None = None) -> None: ...   # raises NestedAgentRefusedError
def set_child_agent_depth(depth: int) -> None: ...
def child_agent_depth() -> int: ...

class NestedAgentRefusedError(ProviderError):   # lionagi/providers/_provider_errors.py
    retryable: ClassVar[bool] = False
```

**Exact semantics.**

- **Every agent lionagi starts carries a depth.** The spawn sets `LIONAGI_AGENT_DEPTH` in the
  child's environment to `child_agent_depth()`: `1`, unless `li agent` launched a profile named in
  `LIONAGI_SEAT_PROFILES` in this process, which sets `0`. It is set over whatever the
  environment held: an interactive shell can carry `0`, and an agent a program in that shell
  starts through the Python API is still an agent lionagi started, so it is handed `1`.
- **Inside an agent is read two ways.** The depth this process inherited, read once at import
  (so `li agent`'s own stamp before its spawn does not refuse it), and, when that is `0`, the
  environment each ancestor started with, read through psutil up to pid 1. An ancestor whose
  environment cannot be read (another user's, one that exited, or on macOS a platform binary such
  as `/bin/sh`) is passed over; the agent CLIs that carry the depth are not such binaries.
- **Refused at every entry, before anything starts.** `ndjson_from_cli()` checks first, before
  admission, so a nested agent never waits for a slot an ancestor holds. An agent handed depth
  `0` may start others, so its children pass that check; an admission that finds a pool full of
  slots held for its own ancestors is refused at once, since those slots free only when the
  ancestor ends. `li agent` (API-model
  legs included), `li o flow` and `li o fanout` check before they stamp their own depth
  and before any run exists.
- **Checked where the request is made, before anything detaches.** `li o flow --background` and
  the lion MCP server's `agent.submit`, `flow.submit`, `fanout.submit` and `play.submit` check in
  the process that was asked. The detached run checks again, but when the depth reached the
  asking process only through its ancestry, a launcher that exits first leaves the run outside
  that ancestry. The MCP server answers with the ADR-0106 kind `conflict` (well-formed, refused
  for where the caller runs) and starts no job.
- **A run the Studio daemon would start is refused at the request.** The daemon runs outside the
  asking process's tree, so the spawn it makes cannot see where the request came from. `li
  schedule create` (both forms), `enable`, `trigger` and `apply`, the lion MCP server's
  `schedule.create`, `schedule.trigger` and `schedule.enable`, and `li studio` itself refuse
  before the request leaves or the daemon starts; the MCP verbs answer `conflict`, the commands
  exit `1`. Disabling or deleting a schedule starts nothing and stays allowed. A request sent to
  the daemon's HTTP API by other means is outside what lionagi's entry points can check; the
  machine-wide caps still bound the agents it starts.
- **The refusal is final from where it was made.** `NestedAgentRefusedError` is a provider error
  with `retryable = False`, not an admission refusal; `li agent` and `li o` print it on one line,
  naming the process that carries the depth, and exit `1`, not `75`.
- **What stays allowed.** A session nobody started through lionagi (a Claude Code or Codex
  session opened by hand) carries no depth and may start agents and use the lion MCP server as
  before. An agent lionagi started may be given MCP servers; what it cannot do is start an agent
  through lionagi, by its shell or through one of those servers.

**Why this way.** The caps bound how many agents run; this bounds how deep they start each other,
whether or not a cap is set (P5). The two reads cover each other's gap: codex starts an MCP server
with only the variables its config names, so a `li agent` that server runs inherits no depth while
the codex process above it does; a process that detaches (`nohup`, `setsid`, launchd) loses its
ancestry but keeps its environment.

**Limits.** A process that both clears its environment and leaves the agent's process tree
escapes both reads; the caps still bound it. A check in the agent harness itself (a pre-tool hook
that refuses `li` commands inside an agent) can add a third layer outside lionagi.

## Consequences

- An install with no cap set behaves exactly as before: no file, no lock, no wait.
- A cap applies to every lionagi process that shares one `LIONAGI_HOME`. Two processes with
  different `LIONAGI_HOME` values count separately, as if on separate machines.
- A capped agent can wait up to ten minutes by default before its first output. lionagi's own
  first-output liveness window opens at admission, so a waiting worker is not read as a silent
  one; only the caller's total deadline runs during the wait. Bounds outside lionagi (a launcher
  timeout, a watchdog reading the process) must allow for the wait or set a shorter one; the wait
  is in front of the child, not inside it.
- A refusal is a retryable failure with its own exit status. A launcher that treats every nonzero
  exit as the agent's fault will misreport refusals until it reads 75.
- `light` changes what an agent can do. A profile marked `light` whose work needs writes outside
  its directory, or the network, fails in ways it did not before; the codex refusal names the
  setting at construction, but a network call simply fails.
- Reversing D1-D4 removes one call and one module; nothing else depends on the slot files. D5 is a
  public field and flag, removed only with a deprecation.
- An agent lionagi started can no longer run `li agent` or `li o`, or a lionagi program
  that starts a CLI agent (D7). A test suite run by such an agent is affected the same way where
  a test starts lionagi in a subprocess.

## Alternatives considered

### A cap in the launcher (flow, fan-out, scheduler)

Each launcher already has a concurrency setting. Raising them to be machine-aware would need every
launcher to share a counter, and agents started by direct library use or a hand-run `li agent`
would still go uncounted (P1). Rejected: the spawn site is the only point all of them pass.

### A broker process that grants tokens

A daemon (the Studio server, say) could hand out slots and queue waiters fairly. It would give
priority and wake-ups instead of polling. It would also make every agent's start depend on that
daemon being up, reachable and correct, and a crashed holder's slot would need the broker to notice
the crash. Rejected: kernel-held locks release on crash with no process watching, and an install
with no daemon keeps working.

### One counter file under one lock

A single file holding a count or a pid list is simpler to read. Every holder that dies without
decrementing leaves the count wrong, so it needs a reaper, and a reaper keyed on pids is fooled by
pid reuse. Rejected: one lock per slot is released by the kernel at exit, and the slot's record is
checked against pid and start time rather than pid alone.

### POSIX named semaphores

`sem_open` gives a counted semaphore across processes. A process that dies holding one does not
release it, and macOS lacks `sem_timedwait` and a usable `sem_getvalue`, so neither waiting nor
status could be built on it. Rejected.

### Letting the child hold the lock

Passing the locked descriptor to the child would tie the slot to the child's life with no record.
A child that spawns a daemon passes the descriptor on, and the daemon then holds the slot for as
long as it runs, long after the agent ended. Rejected: the lock stays with the lionagi process, and
the record covers the case where the child outlives it.

### Counting agent processes by scanning the process table

Matching `ps` output for `codex` or `claude` needs no state. Process names and command lines are
not identity (anything that mentions the binary matches, and an agent can run under another name),
and a scan cannot take a slot atomically: two scans can both see room. Rejected.

### Exit 1 for a refusal

Keeping one failure status is simpler for scripts that only test success. It makes a capped spawn
look like a failed agent, and the remedy for the two is opposite: retry later versus read the run.
Rejected in favour of `EX_TEMPFAIL`.

### `light` as a separate budget only, without confinement

A class that only chooses a pool is easy to add to every engine. It also lets any agent claim the
roomier pool by declaring itself `light`, and "light" would describe nothing the agent is held to.
Rejected: `light` buys its room by being confined, on each engine as far as that engine can be.

### Claude Code `light` as plan mode

Plan mode stops every tool with effects, Bash included. A reader that cannot run a search command
cannot do its job. Rejected in favour of removing the file-writing tools and stating that Bash
remains.

### Pi `light` that silently runs as `heavy`

Running the agent anyway would keep callers working, while it runs unconfined and is counted as
something it is not. Rejected: a refusal says which class the agent has to be.

### A nested agent that runs on its ancestor's slot

A nested agent could borrow the slot its ancestor holds instead of waiting for one, which ends the
wait-for-your-own-parent case. It makes nesting work rather than refusing it, and leaves depth
unbounded. Rejected in favour of D7.

### No MCP servers for `light` agents

Withholding MCP servers from `light` codex agents would close one route to a nested `li agent`.
MCP servers are tools an agent may legitimately need, whatever its class, and the route is
closed for every class by D7. Rejected.

### Reading only the environment, or only the ancestry

The environment alone misses an MCP server started with a scrubbed environment; the ancestry
alone misses a detached process and costs a process-table walk on every spawn even where the
environment already answers. Rejected: the environment is read first and the ancestry only when it
says `0`.

### A fair queue for waiters

Waiters retry independently, so under a sustained full pool an agent can wait while a later one
admits. A queue would order them, but it needs a shared structure that survives crashes, which is
the broker again. DEFERRED: if starvation is measured, a queue file of `(since, pid)` entries under
the same per-slot discipline is the candidate design.

## Notes

The engine pool name is the engine string lower-cased (`codex`, `claude`, `gemini`, `pi`); the
variable upper-cases it and maps `-` to `_`.
