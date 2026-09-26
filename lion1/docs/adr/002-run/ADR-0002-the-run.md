---
adr: ADR-0002
status: draft
liveness: operating (the code still says round; the reference-only wait and the reap at the end are owed, S4)
date: "2026-09-23"
area: run
kind: new
depends_on:
  - "[[ADR-0001-the-actor|ADR-0001]]"
tags:
  - adr
  - runtime
---

# ADR-0002: The run

## Context

A model working on a task speaks in turns, and one message can ask for several things at once. The
runtime has to decide what a message causes to happen, when the next turn begins, and how the work
ends: done, out of budget, refused, or stopped from outside. Left implicit, each is decided by
accident: a turn that asks for nothing ends the job, or never does. This record fixes the loop and
the four ways it ends.

`actor.run(profile, job, backend)` is the actor of [[ADR-0001-the-actor|ADR-0001]] working under one
profile. The job and its bounds are [[ADR-0003-the-bounds|ADR-0003]], the turn's grammar
[[ADR-0004-the-language|ADR-0004]], the order a command goes through
[[ADR-0005-command-handling|ADR-0005]], the notification [[ADR-0006-the-notification|ADR-0006]].
Source: `Actor.run` in `lionagi/actor.py`.

## Definitions

- **run**: the record of a single process from start to completion: one `Actor.run`.
- **turn**: one step of the loop: the runtime's notification, one message from the model, and the
  runtime's reactions to it until they settle; counted when the backend is called.
- **message**: what the model wrote in a turn: prose with requests in it
  ([[ADR-0004-the-language|ADR-0004]]).
- **idle turn**: a turn whose message holds no request.
- **retry**: a turn whose message the runtime did not accept: unreadable, refused whole, or an
  output refused by `accept`; the reason rides the next notification.
- **`Continue`**: the run's state after a settled turn without `OUT{}`; not an outcome.
- **outcome**: the run's terminal value (C4).

## Assumptions

| #  | statement                                                                                                  | source                                                                                     | if false                                          |
| -- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------- |
| A1 | The model decides when it is done; the runtime infers nothing from an idle turn.                | the idle-turn note and the bench's settle rule both had to be built as explicit signals    | C1's idle row becomes an implicit end             |

## Claims

### C1: The turn rules are a closed table _(enforced: mechanical)_ ^c1

- **Subject**: every turn of every run.
- **Violated when**: a turn executes or ends in a way that is not a row below.

| when, first row that fits                                 | what executes                                       | outcome                                                          |
| --------------------------------------------------------- | --------------------------------------------------- | ---------------------------------------------------------------- |
| a bound is spent at the turn's start                      | nothing                                             | `Exhausted(reason)`, unless `run.more` was granted in an earlier turn (ADR-0003) |
| `stop()` is true after the notification                   | nothing                                             | `Stopped(n)`                                                     |
| the message carries the loop's own `<system round=` frame, or two `OUT{}` | nothing                             | a retry; the error rides the next notification                   |
| the message has no LNDL                                   | nothing                                             | the run continues, with an idle note                             |
| the message has `OUT{}` and its fields do not assemble | every command; `OUT{}` waits, then fails to assemble; the turn settles | a retry; the error rides the next notification, a repeat counted (C3) |
| the message has `OUT{}` and `accept` refuses it | every command; `OUT{}` refused; the turn settles | a retry with the reason; `Refused` after `max_refusals` in a row |
| the message has `OUT{}` | every command; `OUT{}` waits for what it references, then assembles | `Success` when the fields assemble and `accept` has nothing to say; the rest cancelled and reaped |
| the message has no `OUT{}`                                | every command                                       | the run continues once they settle                               |

Every command in a turn is dispatched, `OUT{}` or not: a model that writes "edit, then done" in one
turn means both. A command is admitted at alias reservation, entered when its handler is called,
settled when its RESULT lands; one cancelled before it entered is recorded by the loop. A construct
that cannot be read is dropped and named while the rest runs
([[ADR-0004-the-language#^c3|ADR-0004/C3]]); only the frame and a second `OUT{}` refuse the message
whole.

`OUT{}` waits for what it references, through pointers, and for no effect
([[ADR-0005-command-handling#^c6|ADR-0005/C6]]); "edit, then done" points `OUT{}` at the edit. An
accepted output ends the run, and what `OUT{}` did not name is cancelled and reaped (C4); a refused
one is a retry, and the turn's commands settle before the next backend call, so none outlives its
turn.

### C2: A command alias is bound once; a value alias is rebound by a later declaration _(enforced: mechanical)_ ^c2

- **Subject**: every alias a turn declares.
- **Violated when**: two live bindings share a name, a collision is resolved silently, or a
  generated alias is refused.

Within a turn the first use of an alias wins and the second is named. Across the run a re-declared
value replaces the earlier one and drops its output binding; a command alias colliding with anything
is refused and named. A generated `auto_` alias from a native tool call is re-suffixed instead,
because the model never chose it.

### C3: Repeats are counted, and stop is asked after the notification _(enforced: mechanical)_ ^c3

- **Subject**: every turn.
- **Violated when**: the same error on consecutive turns is told without its count, an identical
  turn is not named, or `stop()` is consulted before the turn's sections have run.

The notification says "the same error N turns running" and "your last turn was identical to the one
before it (N times now)", so the next turn has a reason to differ. The sections have just looked at
the world, and what they saw is what a stop decides on; asked before, the model got one more turn
after every notice that the run was over. The run ends `Stopped(n)` with n the turn that did not
run: the stopped iteration called no backend and is not a turn.

### C4: A run ends in one of four values, and nothing else is an outcome _(enforced: mechanical)_ ^c4

- **Subject**: every run.
- **Violated when**: `run.outcome` holds a value outside the four, or a retry, a refusal or a fold
  is reported as one.

`Success(output)`, `Exhausted(rounds, reason)` with the reason rounds or time today (idle, cost and
context land with their bounds, [[ADR-0003-the-bounds#^c2|ADR-0003/C2]]), `Refused(why, times)` and
`Stopped(round)`. `Continue`, a state and not an outcome, is what a settled turn without `OUT{}`
leaves; a retry shows as `retry_error` and the `round.retry` event, never as an outcome.

Pending tasks and open asks are cancelled when the run ends, whichever value it ends in. Awaiting
them before `run` returns, so every admitted command has its RESULT when the caller reads the
record, is owed (S4). An exception the loop does not catch, a backend that raises, a store that
cannot be written or a command task's own defect (S5), propagates from `run` to the caller with
`run.outcome` unset; the record holds what was appended. That is an abort, not an outcome.

## Decisions

### D1: The loop is `Actor.run` as written ^d1

Serves C1 to C4. Bounds are checked at the turn's start, the inbox is taken, the notification is
appended, `stop` is asked, the backend is called once, the message is read, every command is
dispatched, and the turn settles. The caller reads `run.outcome` after the loop returns.

- **Landing evidence**: `tests/test_actor.py`; tests compare `Exhausted`, `Refused` and `Stopped` by
  value and `Success` by type. Owed: a cancelled command's RESULT is on the record when `run`
  returns, the never-started one written by the loop; the closure entry (ADR-0006/C1).

## Alternatives

| approach                                                | rejected because                                                                                     |
| ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `OUT{}` as the only execution trigger                   | a message without `OUT{}` becomes a no-op, so the model cannot look before it decides                  |
| Commands beside `OUT{}` as scratch                      | told three times that its edit had not run, a model wrote the same message again and its fix never landed |
| `stop` asked before the notification                    | one extra model turn after every notice that the run was over                                        |
| An idle turn as the end of the run                     | the model was never asked; the idle note and `max_idle` say it instead (ADR-0003)                    |

## Consequences

- **S1**: A retry costs a turn and appears in the record as an event; a caller counting outcomes
  never sees it.
- **S2**: The idle row keeps a talking model alive; `max_idle` (ADR-0003) is what ends it.
- **S3**: The code still says round where this corpus says turn: `max_rounds`, `Exhausted(rounds)`,
  `<system round=`, "rounds running", the `round.*` events, `Stopped(round)`. The rename is owed
  with the first change that touches them; the `Retry` class the code defines and never constructs
  goes with it.
- **S4**: The loop today gathers every command of the turn before `OUT{}` assembles; the design
  waits for the references only, and the gather goes with the first change that touches it. A wait
  for the whole turn is at most an opt-in, later. The end of the run cancels without awaiting today,
  so a cancelled command's RESULT lands after `run` returned; the reap is owed with the same change.
- **S5**: A command task that ends in neither its RESULT
  ([[ADR-0005-command-handling#^c1|ADR-0005/C1]]) nor a cancel is the runtime's own defect: the
  turn's settle raises it and never drops it, and the run aborts (C4). No test pins it.
