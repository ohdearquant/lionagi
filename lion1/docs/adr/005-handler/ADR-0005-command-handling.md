---
adr: ADR-0005
status: draft
liveness: operating (the replacement rebuilt from its fields, the three captures, the effect wait and the `get` refusal are owed)
date: "2026-09-23"
area: handler
kind: new
depends_on:
  - "[[ADR-0003-the-bounds|ADR-0003]]"
  - "[[ADR-0001-the-actor|ADR-0001]]"
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0004-the-language|ADR-0004]]"
tags:
  - adr
  - runtime
---

# ADR-0005: Command handling, the bus and hooks

## Context

Each thing the model asks for goes through one order: checked, permitted, gated, run, reported. The
loop must not know what the things are, so adding one is one function and none of them blocks a
turn. The person running the model over a directory must be able to refuse a thing before it runs,
or watch it after, without writing Python. This record fixes that order and those two hooks.

A turn's commands ([[ADR-0004-the-language|ADR-0004]]) are dispatched by the actor inside a turn
([[ADR-0002-the-run#^c1|ADR-0002/C1]]). This record fixes the order a command goes through, the bus
that observes it, the privilege gate, what a handler is given, and the hooks around a handler.
Source: `Actor._dispatch`, `Actor._perform`, `Actor._execute`, `Context` and `Handler` in
`lionagi/actor.py`; `lionagi/bus.py`; `hub/hooks/`.

## Definitions


- **dispatch**: the order a command goes through (C1).
- **`Context`**: what a handler is given beside its request: the run, the alias being settled, the
  profile, `get(name)` to dereference any pointer, the note store, `emit` for further events, and
  `direct` for the context commands.
- **result**: what a handler returned, or its failure with a reason; a `Result` value in code.
- **`RESULT`**: the record entry the result lands on, under the command's alias.
- **execution**: the RESULT's mark of whether the handler was called: `not executed`, `started`, or
  `completion unknown` when cancelled after the call; set by the loop, never by a handler or a hook;
  it says what the loop did, never what the world did.
- **reason**: the closed set on a failed `RESULT`: `unknown`, `invalid`, `privilege`, `refused`,
  `failed`, `cancelled`.
- **bus**: the in-process observer of events; it records and notifies, and decides nothing.
- **before hook**: a function or a shell command asked before a handler runs; it lets the command
  run, refuses it with a reason, or replaces its arguments.
- **after hook**: a function or a shell command shown what ran and its result; it may replace the
  result, never undo the command.
- **turn hook**: a shell command run once per turn whose stdout is a notification line; the `round`
  table in `hooks.toml` until that name is changed.

## Assumptions

| #  | statement                                                                  | source                                                                                                              | if false                                                  |
| -- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| A1 | A failed command is worth one turn of the model's reading, never the run. | on the bench, 4 edit errors in one harness and 8 in another were all followed by a corrected turn ([[ADR-0012-the-bench|ADR-0012]], the bench)        | C1's failed-RESULT rule should end the run on some errors |
| A2 | Commands in one turn are independent enough to run concurrently.          | measured once: two edits to one file in one turn, the second saw the first's change (`test_two_edits_to_one_file_in_one_turn_both_land`); the order was the scheduler's, nothing promises it | the dispatcher must order commands within a turn |
| A3 | A hook's verdict fits an exit code and a line of stdout.                   | the tools people already use (git hooks, editor hooks)                                                              | a JSON verdict on stdout, the exit code kept              |
| A4 | Thirty seconds bounds a hook; a slower one is a job, not a hook.           | the default timeout, per hook in the file                                                                           | the file sets `timeout`                                   |

## Claims

### C1: One order per command _(enforced: mechanical)_ ^c1

- **Subject**: every `<lact>` the loop dispatches.
- **Violated when**: a step is skipped, or a refusal carries no reason from the closed set.

The name is in the profile's subset, else `unknown`. Pointers are dereferenced, waiting on any still
running, then the effect wait (C6); a pointer to nothing is `invalid`, so is one leading back to its
own command. The arguments validate against the Spec, else `invalid`. The handler's `requires` is
within the profile's privileges, else `privilege`. The before hooks run (C4), else `refused`. The
handler runs, and the after hooks see its result.

The return lands on the record as `RESULT` under the command's alias, with its `execution` mark; a
handler that raises produces a failed `RESULT` (`failed`, with the exception) and never breaks the
loop. One that stops without returning settles too: `cancelled` when its work was cancelled
underneath it, `invalid` when a validator's own exception refused the arguments. `pending` drops the
alias on every exit.

### C2: Nothing is awaited inline; the bus observes _(enforced: mechanical)_ ^c2

- **Subject**: every `Bus.emit` and every dispatch.
- **Violated when**: `emit` awaits a subscriber's coroutine inline, a subscriber's exception reaches
  the emitter, or the loop awaits a handler anywhere but the turn's settle.

Each command runs as its own task; the turn settles by gathering them. The bus is an in-process
observer: `emit` records the event, calls each matching subscriber in turn, schedules any coroutine
a subscriber returns as a tracked task, and keeps a subscriber's exception in `errors`. A subscriber
that blocks holds the emitter, so a subscriber returns at once or hands back a coroutine; the log is
bounded, the tracked tasks are bounded only by `drain`.

Patterns match exactly, by prefix (`command.`), or everything (`*`). Events: `run.started`,
`round.started`, `round.retry`, `command.requested`, `command.settled`, `inbox.queued`,
`inbox.answered`, `run.completed`.

### C3: A handler reaches run state only through its `Context` _(enforced: mechanical)_ ^c3

- **Subject**: every registration on an actor and every handler call.
- **Violated when**: a handler is registered without a Spec, is called with anything but `(request,
  context)`, or is handed anything but its `Context` by the runtime.

`actor.handler(Spec)` registers a function; any object with a `spec` attribute and a callable
`(request, context)` registers directly.

Everything a handler can do to the run it does through `Context`: `get` waits for a pending pointer,
`direct` moves the view (ADR-0007), `emit` is observed by the bus, and the note store is the
profile's. `Context` is the supported interface, not a sandbox: a handler is trusted Python, and the
claim is over what the runtime hands it, not over what Python lets it reach. The privilege gate that
precedes the hooks is [[ADR-0001-the-actor#^c4|ADR-0001/C4]], the fourth step of C1.

### C4: A before hook decides for the command, and a gate that fails admits nothing _(enforced: mechanical)_ ^c4

- **Subject**: every command past validation and privilege.
- **Violated when**: a handler runs after a hook refused, raised, or timed out, or a refusal reaches
  the model without its reason.

Hooks run in registration order, each seeing the request as those before it left it: a guard after a
rewriting hook judges the rewrite; the order is the operator's.

None lets the command run; a string refuses it (`refused: <reason>`). A dict or an instance of the
Spec replaces the arguments, either rebuilt from its fields, since a class is no receipt and a
stable validator ([[ADR-0001-the-actor|ADR-0001]] S4) makes the build count immaterial; another
class is refused. Changing the request in place changes nothing; a returned replacement counts (C5).
A hook that raises, exits non-zero or times out refuses the command.

A hook runs in its own process group; a timeout or a cancelled run ends it. `OUT{}` is the `out`
command, `accept` its last before hook (ADR-0003).

### C5: An after hook sees what ran, ok or not, and cannot undo it _(enforced: mechanical)_ ^c5

- **Subject**: every command whose handler was called.
- **Violated when**: a refused command reaches an after hook, or an after hook's failure changes the
  result.

The hook gets the request and the result; None keeps the result, a Result replaces it. A hook that
raises is listed in the next notification under `hooks failed` (ADR-0006), and the result stands:
the command already ran.

The runtime captures three values: the request as the handler entered, the handler's return before
the first after hook, and the result as the hooks left it. Each hook works on a copy; a replacement
is captured when returned, a failed hook's copy is dropped. The RESULT holds the third, what a
pointer dereferences; the first two ride the entry as provenance, out of the model's reach. A
capture shares an immutable value and copies a list or dict
([[ADR-0007-the-record#^c1|ADR-0007/C1]]).

### C6: A handler's effect class orders the turn _(enforced: mechanical)_ ^c6

- **Subject**: every command dispatched in one turn, hooks included.
- **Violated when**: a command runs before one its class waits for has settled, a `pure` command
  touches live state, or a cycle over pointer, control and effect edges is dispatched.

A handler declares `effect`: `write` (the default), `read` or `pure`, for the whole invocation. In
source order a `write` waits for every earlier command of the turn but the `pure` ones, a `read`
waits for the nearest earlier `write`, a `pure` command waits for its pointers only. Reads overlap
reads, and every command sees the state the writes before it left. `OUT{}` waits for its references
and no effect ([[ADR-0002-the-run#^c1|ADR-0002/C1]]).

The rule orders one run's commands, not transactions: a read-modify-write across two commands is not
isolated, and two runs on one directory share nothing. The class is the author's word; A2's
measurement is what checks it.

## Decisions

### D1: Dispatch as one task per command; `Actor.before` and `Actor.after` beside `Actor.section` ^d1

Serves C1 to C5. `_dispatch` creates a task per `<lact>` and records it under the alias in
`pending`; `_perform` walks C1's order and appends the `RESULT`; `_execute` calls the two hook lists
around the handler. Every holder of an alias is checked at dispatch and `pending` is empty at each
turn's end, so no RESULT finds its alias taken at settle; the branch for that case tells the call
under its `execution` heading. The runtime knows nothing of files or shells.

- **Landing evidence**: `tests/test_actor.py`, `tests/test_actor_hooks.py`,
  `tests/test_swe_solve.py`. Owed: a replacement rebuilt from its fields (today any model class is
  taken as it comes); the three captures; the effect wait (C6), with the class on `Handler`.

### D2: The bus is `lionagi/bus.py` ^d2

Serves C2. One class, three operations: `subscribe`, `emit`, `drain`; a bounded log of events.

- **Landing evidence**: the event log of any recorded run.

### D3: `hub/hooks/`: `hooks.toml`, shell commands, JSON on stdin, the exit code as the verdict ^d3

Serves C4 and C5 for a person. Three array tables, `before`, `after` and `round`, each row with
`run`, optional `name`, `match` (fnmatch over the command name) and `timeout`. Exit 0 lets a command
run and a JSON object on stdout replaces its arguments; exit 2 refuses with stdout as the reason; a
turn hook's stdout is a notification line. `lion chat` reads `.lion/hooks.toml` in its directory,
`lion agent` the agent directory's `hooks.toml`. A `python -m hub.hooks.scripts.<name>` row runs
under `-I` with lion's own interpreter.

- **Landing evidence**: `tests/test_hooks.py`; the console test that installs a directory's hooks.

## Alternatives

| approach                                             | rejected because                                                                    |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Native tool calling as the command channel           | a provider's wire format; several calls per response exist, but a pointer from one call into another has no native form (ADR-0004) |
| Governance in the loop                               | every new capability touches the loop; here it is a `requires` set on a handler     |
| Durable-effect plumbing (Executor, Action, Envelope) | none of it is needed for a handler that returns a value                             |
| Bus events for the gate                              | an event cannot refuse; a gate must, and must fail closed                           |
| Hooks in Python only                                 | the person at the console has a directory and a shell, not a Python package        |
| A shell after hook that rewrites the result          | a shell verdict on a value is a second parser; a Python after hook does it          |
| Effect domains named by resource (a path, a key) | on the bench three classes save 17% of the serial steps and no turn needed a name; names are a later record when a measured block asks for them (S7) |
| One class, ordered or pure | reads are one command in three on the bench and overlap only under a `read` class (S7) |


## Consequences

- **S1**: A tool is a handler and nothing more; the bus log is the run's trace.
- **S2**: A failed or refused command costs the model one turn to read, never the run; the reason is
  in the result. The exceptions are the job's: an output refused `max_refusals` times in a row, and
  a bound `extend` would not raise ([[ADR-0003-the-bounds|ADR-0003]]).
- **S3**: A slow hook slows every command it matches; `match` and `timeout` are the person's levers.
- **S4**: The reference file `hub/hooks/hooks.toml` carries `no-secrets` over `read*` and `search`,
  `guard-bash`, `audit`, `clock` and `tree-changed`. A path or glob that looks like a secret is
  refused unless git tracks it. The bash guard refuses the network into a shell, a recursive delete
  of the tree or above it, and `git checkout .` with its kin, reading git's `-C`, `-c`, `--git-dir`,
  `--work-tree` and `--` before the subcommand.
- **S5**: A directory's `hooks.toml` is executable configuration: `lion chat` runs what the
  directory holds, so opening a directory is trusting its hooks, as with any tool that reads a local
  config.
- **S6**: `Context.get` on a pending alias waits without the cycle check `_deref` applies to a
  command's arguments, and time is read only at the turn's start, so two handlers waiting on each
  other through `get` hold the run for good. Decided: `get` reaches settled entries and the
  command's own pointers; a pending alias the command did not point at is refused, so every wait is
  an edge the check at dispatch has seen. Owed, with a deadline beside the wait (ADR-0003/S3).
- **S7**: On the 50-instance bench, 1722 commands in 733 turns: `run` 982, ordered whichever way,
  and 609 reads; three classes save 253 of 1458 serial steps over two on the 469 turns with two or
  more commands, 17%. One turn wrote 81 commands.
