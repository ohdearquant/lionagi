---
adr: ADR-0003
status: draft
liveness: operating (every bound resolves once at run start and is checked at the turn's start, time and cost again before each handler; the context check and its job error, run.more, extend, the profile's defaults, out as a command and accept as its last hook are under tests; the notification does not name idle turns, cost or refusals, and no backend hands back a cost yet, S4)

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
  `extend`, the bounds, and for a conversation `wait` and `history`
  ([[ADR-0018-chat-in-a-box|ADR-0018]]); `RunRequest` in code.
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

In code a job leaves each bound None, and the run records the resolved values as `run.bounds`; with
neither the job nor the profile setting it, `context_budget` is 200,000. `view_budget`, 60,000, is a
job field and no bound ([[ADR-0007-the-record|ADR-0007]]) (D1).

### C2: Every bound is checked at the turn's start; time and cost also between commands _(enforced: mechanical)_ ^c2

- **Subject**: every turn and every dispatched command.
- **Violated when**: a spent bound admits a new turn, or a command's handler starts after
  `time_budget` or `cost_budget` is spent.

The turn's first act is the check: elapsed time, reported cost, turns, idle turns and the context
figure against their bounds, so a deadline on the last turn reads time. Time and cost are re-read
once a conversation's `wait` answers and before each handler starts, after its pointer waits; one
running at the deadline is cancelled beside the turn's wait (S3). `max_idle` counts consecutive idle
turns; any turn that is not idle resets it.

`context_budget` compares the anchored figure ([[ADR-0006-the-notification#^c4|ADR-0006/C4]]) as the
last notification left it; over it, the run ends `Exhausted(context)`. A job over it before any
backend call is refused as a job error, not a run that ended. The fold runs in the next
notification, after this check; `view_budget` below the budget ([[ADR-0007-the-record|ADR-0007]])
keeps a crossing rare.

### C3: A bound is not an ambush: the model is told what is left, and may ask for more _(enforced: mechanical; owed, S4)_ ^c3

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

Today the notification names turns, time and the context figure against their bounds; idle turns,
cost and refusals are not named (S4).

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

- **Landing evidence**: `tests/test_actor.py`, each red when what it pins is removed or changed:
  `test_every_bound_resolves_once_at_run_start_the_job_over_the_profile_over_the_runtime`,
  `test_max_rounds_defaults_to_a_hundred`,
  `test_idle_turns_in_a_row_end_the_run_and_any_other_turn_starts_the_count_over`,
  `test_a_time_budget_ends_the_run_as_exhausted_by_time_and_the_header_shows_the_clock`,
  `test_a_wait_that_outlasts_the_time_budget_admits_no_further_call`,
  `test_a_handler_running_at_the_deadline_is_cancelled_with_the_one_waiting_on_it`,
  `test_reported_cost_ends_the_run_and_a_command_asked_once_it_is_spent_never_starts`,
  `test_a_reported_cost_that_is_not_a_number_reads_as_spent`,
  `test_the_context_figure_the_last_notification_told_ends_the_run_over_its_budget`,
  `test_a_job_over_its_context_budget_before_any_call_is_refused_as_a_job_error`,
  `test_repeated_refusals_end_the_run_as_refused`.

### D2: `run.more` is a default handler; `extend` is a job field whose default grants ^d2

Serves C3. The handler reads the ask, calls `extend(run, bound, amount, reason)`, and on a grant
raises the run's resolved bound; the next turn's check reads the raised value. The default `extend`
grants. When an actor with the authority exists (a chair, a department head), the caller passes an
`extend` that asks it; nothing in the runtime changes.

- **Landing evidence**: `tests/test_actor.py`:
  `test_a_granted_ask_for_more_rounds_continues_past_max_rounds`,
  `test_a_refused_ask_leaves_the_bound_and_the_run_ends_at_the_cap_not_at_the_refusal`,
  `test_two_asks_in_one_turn_are_answered_in_order_against_one_total`,
  `test_an_ask_on_an_unbounded_bound_raises_nothing_and_only_a_count_is_rounded_up`.

### D3: `out` is registered like any handler, with `accept` appended to its before hooks ^d3

Serves C4. Dispatch assembles and validates `out` before its before hooks. The run's `accept`, when
set, is their last hook, after the actor's, so it judges the rewritten output. Hooks registered on
the actor see `out` like any other command; an after hook's replacement on `out` is refused and
listed under `hooks failed`, so the caller receives the value `accept` approved (a change made in
place by an after hook is ADR-0005/S8's copy, owed).

- **Landing evidence**: `tests/test_actor.py`:
  `test_accept_gate_refuses_then_lets_a_later_turn_through` reads `refused:` in the next
  notification; `test_a_before_hook_sees_out_and_its_rewrite_is_what_accept_judges`,
  `test_a_before_hook_refusing_out_is_a_refusal_the_profile_default_counts`,
  `test_an_after_hooks_replacement_of_out_is_refused_and_listed`. The loop no longer calls `accept`.

### D4: A rewrite of `out` is held by the rule that held `OUT{}`, and `accept` judges it held ^d4

Serves C4. A before hook's rewrite is held to the run's outputs by the rule that held `OUT{}`: a
field off the list, a required output dropped, or a value its output refuses is a refusal. `accept`
judges the rewrite once held and validated, so it sees output instances, never a rewrite the outputs
refuse, and the caller receives the value it judged.

- **Landing evidence**: `tests/test_actor.py`:
  `test_a_before_hooks_rewrite_of_out_is_held_to_the_runs_outputs`,
  `test_a_before_hooks_rewrite_may_drop_an_output_whose_fields_all_default`,
  `test_accept_judges_the_rewrite_validated_and_the_caller_receives_what_it_approved`,
  `test_accept_is_not_asked_about_a_rewrite_the_outputs_refuse`.

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
  time, about one command's duration. The check admits and reserves nothing: handlers admitted
  together can each spend the one remainder.
- **S2**: The default grant is trust, not a gate: the granting `extend` belongs to an actor with the
  authority once one exists, never to a human on call.
- **S3**: The remainder is reported, not a final warning: a bound spent inside a backend call or a
  command ends the run at the next check, so a model that wants a last `OUT{}` asks for more while
  there is room. A command that never returns is cancelled at a deadline beside the turn's wait,
  what `time_budget` leaves; unless the turn's `OUT{}` was accepted, the next check ends the run
  `Exhausted(time)` (ADR-0005/S6), on the last turn too; a refusal cap keeps `Refused`.
  `test_a_command_that_never_returns_is_cancelled_at_the_deadline_and_the_run_ends_by_time` and
  `test_a_deadline_on_the_last_turn_ends_the_run_by_time_not_by_rounds` pin it.
- **S4**: D1-D3 landed with their tests: bound resolution and every check (C1, C2), extension
  requests and grants (C3), and output dispatch through hooks (C4). Still owed with C3: the
  notification naming idle turns, cost and refusals with their remainders. `Reply` carries a cost
  and the run sums it, but no backend hands one back yet, so `cost_budget` reads nothing spent; a
  cost that is not a number reads as spent, never as room
  (`test_a_reported_cost_that_is_not_a_number_reads_as_spent`). `RunRequest` takes every field by
  name, so a new bound never shifts another (`test_a_run_request_takes_its_fields_by_name_only`).
- **S5**: A grant of time that lands after the deadline passed raises the bound for what follows the
  turn. Each command the turn still has pending is refused before its handler starts, whether the
  turn's wait held it or `OUT{}` left it out: one queued behind a synchronous grant that held the
  loop past the deadline would otherwise find room at the C2 re-read. A program's commands also
  answer to their walk's limits (ADR-0008/S15).
  `test_a_command_queued_behind_a_held_grant_in_a_tag_turn_never_enters_its_handler` and
  `test_a_command_an_out_turn_leaves_out_of_its_wait_is_refused_after_a_late_grant` pin it.
