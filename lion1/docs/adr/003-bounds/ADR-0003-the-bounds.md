---
adr: ADR-0003
status: draft
liveness: partial (max_rounds, time_budget, max_refusals and accept operate; max_idle, cost_budget, the context check and its job error, run.more, extend, the profile's defaults, out as a command and accept as its last hook are decided here and owed)

date: "2026-09-23"
area: bounds
kind: new
depends_on:
  - "[[ADR-0001-the-actor|ADR-0001]]"
  - "[[ADR-0002-the-run|ADR-0002]]"
tags:
  - adr
  - runtime
---

# ADR-0003: The bounds

## Context

Every job given to a model needs limits: how many turns, how long, how much money, how much context.
A limit the model cannot see ends the job mid-task and wastes what was spent; a limit the model can
raise with nobody's say is no limit. This record fixes what a job is given, how each limit is
enforced, how the model asks for more, and whose say answers.

The job is the caller's side of `actor.run(profile, job, backend)` ([[ADR-0002-the-run|ADR-0002]]);
in code the job is still named `RunRequest`; this corpus reserves the word request for a signal the
model produces ([[ADR-0001-the-actor|ADR-0001]]). The profile
([[ADR-0001-the-actor#^c3|ADR-0001/C3]]) supplies defaults beneath it. Every bound is told to the
model each turn ([[ADR-0006-the-notification|ADR-0006]]); the view's own fold line belongs to the
record ([[ADR-0007-the-record|ADR-0007]]). Source: `RunRequest` and the bound checks in
`lionagi/actor.py`.

## Definitions

- **job**: what the caller hands to `run`: `inputs`, `emits` (the Spec of the output), `accept`,
  `extend`, the bounds, and for a conversation `wait` and `history` (ADR-0018, chat, not yet
  written); `RunRequest` in code.
- **bound**: a limit the run is checked against: `max_rounds`, `max_idle`, `time_budget`,
  `cost_budget`, `context_budget`, `max_refusals`.

- **`defaults`**: the profile's values for the bounds, taken where the job leaves a field unset.
- **`run.more`**: the default command by which the model asks to raise a spent bound:
  `run.more(bound, amount, reason)`.
- **`extend`**: the job's callable that answers `run.more`.
- **`accept`**: the job's gate on the output beyond validation: it returns why the output is not
  acceptable, or nothing.
- **`out`**: the command `OUT{}` is dispatched as.

## Assumptions

| #  | statement                                                                                                  | source                                                                                     | if false                                          |
| -- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------- |
| A1 | A model asks for more turns in good faith; a default that grants is trust the operator can afford.       | decided 2026-09-23; a human on call per extension is not realistic                         | D2's default `extend` must refuse, or ask a role  |
| A2 | At a hundred turns the ask is rare once a turn carries several commands.                                  | the bench's runs settled well inside; no run near the cap has been measured                | C3's ask is the common path, and its cost matters |

## Claims

### C1: Every bound is a field of the job, resolved once at run start _(enforced: mechanical)_ ^c1

- **Subject**: every run.
- **Violated when**: a bound is enforced that the job does not carry, a carried bound is not
  enforced, a profile default overrides an explicit job value, or a bound is re-read mid-run.

A field the job sets explicitly wins; one it leaves unset takes the profile's `defaults`, then the
runtime's: `max_rounds` 100, `max_idle` 3, `max_refusals` 3, no time or cost budget,
`context_budget` from the profile. The resolved values are recorded on the run before the first turn
and the checks read those. A chair's profile may default to a long context and a desk's to a short
one; the caller of `run` still sizes the task.

### C2: Every bound is checked at the turn's start; time and cost also between commands _(enforced: mechanical)_ ^c2

- **Subject**: every turn and every dispatched command.
- **Violated when**: a spent bound admits a new turn, or a command's handler starts after
  `time_budget` or `cost_budget` is spent.

The turn's first act is the check: turns, idle turns, elapsed time, reported cost and the context
figure against their bounds. Time and cost are re-read before each handler starts, after its pointer
waits; handlers already running finish, so a turn overruns by at most those. The check admits and
reserves nothing: handlers admitted together can each spend the one remainder. `max_idle` counts
consecutive idle turns; any turn that is not idle resets it.

`context_budget` compares the anchored figure ([[ADR-0006-the-notification#^c4|ADR-0006/C4]]) as the
last notification left it; over it, the run ends `Exhausted(context)`. A job over it before any
backend call is refused as a job error, not a run that ended. The fold runs in the next
notification, after this check; `view_budget` below the budget ([[ADR-0007-the-record|ADR-0007]])
keeps a crossing rare.

### C3: A bound is not an ambush: the model is told what is left, and may ask for more _(enforced: mechanical)_ ^c3

- **Subject**: every turn and every `run.more`.
- **Violated when**: a run ends `Exhausted` on a bound the notification never named, or a `run.more`
  is answered by anything but the job's `extend`.

The notification names each bound and its remainder every turn. A bound about to be spent can be
asked for with `run.more` before the check that would end the run. `extend` decides: a grant raises
the bound and the run continues; a refusal leaves the bound as it is and is told, and the run ends
`Exhausted` when the bound is spent.

Two asks in one turn are answered in order against one total; a grant never resets what was spent or
the clock. The default grants, on A1. A profile that must never extend leaves `run.more` out of its
subset.

### C4: `OUT{}` is a command, and `accept` is its last before hook _(enforced: mechanical)_ ^c4

- **Subject**: every turn with `OUT{}`.
- **Violated when**: `OUT{}` is validated or refused by a path the other commands do not go through,
  or `accept` runs before the fields have assembled.

`out` waits for what `OUT{}` references, pending included ([[ADR-0002-the-run#^c1|ADR-0002/C1]]),
assembles the fields and validates them with the `emits` Spec class
([[ADR-0001-the-actor#^c1|ADR-0001/C1]]). The before hooks run as for any command
([[ADR-0005-command-handling|ADR-0005]]), `accept` last, after the operator's rewrites. A refusal is
told and the run continues; only `max_refusals` consecutive refusals end it `Refused`. A later turn
without `OUT{}` resets the count.

`accept` sees the run beside the output, so a job requiring an effect the output does not name reads
the record; the runtime infers nothing from an unreferenced command. The record is a prefix: a
pending receipt is absent, not failed, and the refusal settles the turn
([[ADR-0002-the-run#^c1|ADR-0002/C1]]), so the next attempt sees it. The caller receives what
`accept` approved; an after hook on `out` replaces nothing (D3).

## Decisions

### D1: The bounds are fields of `RunRequest`, resolved once at run start ^d1

Serves C1 and C2. `Actor.run` resolves each unset field from the profile's `defaults`, then the
runtime default, before the first turn, and records the resolved values on the run. The checks read
the resolved values; nothing re-reads the profile.

- **Landing evidence**: owed: `tests/test_actor.py`, one case per bound, each red when its check is
  removed. Today `max_rounds` defaults to 10, `context_budget` is reported and not checked, and
  `max_idle`, `cost_budget` and `defaults` do not exist.

### D2: `run.more` is a default handler; `extend` is a job field whose default grants ^d2

Serves C3. The handler reads the ask, calls `extend(run, bound, amount, reason)`, and on a grant
raises the run's resolved bound; the next turn's check reads the raised value. The default `extend`
grants. When an actor with the authority exists (a chair, a department head), the caller passes an
`extend` that asks it; nothing in the runtime changes.

- **Landing evidence**: owed: `tests/test_actor.py`: a granted ask continues past `max_rounds`; a
  refusing `extend` ends `Exhausted(rounds)` at the cap, not at the refusal; two asks in one turn
  are answered in order against one total.

### D3: `out` is registered like any handler, with `accept` appended to its before hooks ^d3

Serves C4. The `out` handler assembles and validates; the run's `accept`, when set, is the last
before hook for that command only, after the actor's, so a rewrite lands before the judgment. Hooks
registered on the actor see `out` like any other command; an after hook's replacement on `out` is
refused and listed under `hooks failed`, so the caller receives the value `accept` approved.

- **Landing evidence**: owed: `tests/test_actor.py`: a refusing `accept` produces `refused:` in the
  next notification; a before hook rewriting `out` runs before `accept`; an after hook's replacement
  is listed. Today the loop calls `accept` itself and no hook sees `OUT{}`.

## Alternatives

| approach                                                | rejected because                                                                                     |
| ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| A low turn cap (10) as the main bound                  | turns vary a hundredfold in cost; a coding CLI has no turn cap at all; cost and time are the bounds |
| A bound the model cannot see or contest                 | a run that ends mid-task with no warning wastes what it spent; the model asks, `extend` decides       |
| A human approving each extension                        | nobody is on call for a run's hundredth turn; at 100 turns the ask is rare, and a role can grant later |
| `context_budget` reported but not enforced              | the figure is anchored on the provider's count now; the provider's own error is a worse ending        |
| `OUT{}` with a gate of its own outside the hooks        | one path for every command; an operator's hook on the output is the common case, not the exception   |
| Bounds on the profile                                   | a profile is a hat; the caller of `run` knows the task's size, the profile knows the role's habit     |

## Consequences

- **S1**: A bench instance once ran 23 commands a turn and exited at 1225 s on a 1200 s budget; with
  the between-commands check the overrun is what was already running when the bound was spent: for
  time, about one command's duration.
- **S2**: The default grant is trust, not a gate: the granting `extend` belongs to an actor with the
  authority once one exists, never to a human on call.
- **S3**: The remainder is reported, not a final warning: a bound spent inside a backend call or a
  command ends the run at the next check, so a model that wants a last `OUT{}` asks for more while
  there is room. A command that never returns reaches no check, since time is read only at the
  turn's start; a deadline beside the wait is owed (ADR-0005/S6).
