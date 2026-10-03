---
adr: ADR-0002
status: draft
liveness: operating (two OUT{} refusing the message whole, a repeated accept refusal told with its count and a generated alias freed past a value are pinned by tests; the opt-in wait for the whole turn is not built, S4)
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
Source: `Actor.run` in `src/lion1/actor.py`.

## Definitions

- **run**: the record of a single process from start to completion: one `Actor.run`.
- **turn**: one step of the loop: the runtime's notification, one message from the model, and the
  runtime's reactions to it until they settle; counted when the backend is called.
- **message**: what the model wrote in a turn: prose with requests in it
  ([[ADR-0004-the-language|ADR-0004]]).
- **idle turn**: a turn whose message holds no request.
- **retry**: a turn whose message the runtime did not accept: unreadable, refused whole, an output
  that could not be computed or assembled, or one refused by `accept` short of `max_refusals`; the
  reason rides the next notification.
- **`Continue`**: the `Outcome` value a settled turn without `OUT{}` leaves in `run.outcome`; never
  a run's terminal value.
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
| under `wait`, after a reply or a pause with no command pending, `wait` answers false at the turn's start | nothing, not even the notification | `Stopped(n)`                                     |
| `stop()` is true after the notification                   | nothing                                             | `Stopped(n)`                                                     |
| the message carries the loop's own `<system turn=` frame, two `OUT{}`, an unreadable call or program (ADR-0008/C1) or a backend's refusal | nothing | a retry; the error rides the next notification |
| under `wait`, the turn launched no command and reached no `OUT{}`: in the tag form none is in the parsed program; in the program form its walk (ADR-0008) did neither, computing an earlier turn's value included | no command; a value or note it declares is taken   | the run continues as a reply; `wait` is asked at the next turn's start |
| the message has no LNDL, in a run without `wait`          | nothing                                             | the run continues, with an idle note                             |
| the message's commands or `OUT{}` lie only where control flow does not reach: an untaken branch, after a `return`, a short-circuited expression | what is reached; no output | the run continues once they settle |
| the message has `OUT{}` and its fields do not assemble | every command reached; `OUT{}` waits, then fails to assemble; the turn settles | a retry; the error rides the next notification, a repeat counted (C3) |
| the message has `OUT{}` and `accept` refuses it | every command reached; `OUT{}` refused; the turn settles | a retry with the reason; `Refused` after `max_refusals` in a row |
| the message has `OUT{}` | every command reached; a tag-form `OUT{}` waits for what it references; a program's walk settles every command it launched, within the time budget ([[ADR-0008-the-program#^d1\|ADR-0008/D1]]), before `OUT{}` assembles | `Success` when the fields assemble and `accept` has nothing to say; in the tag form the rest cancelled and reaped; in the program form nothing is left, and an unrelated command bounds `Success` by the time budget |
| under `wait` with `reply_on_commands`, the message has commands and no `OUT{}`, every one lands ok and nothing is refused | every command reached | the run continues once they settle, as a reply; `wait` is asked at the next turn's start |
| the message has no `OUT{}`                                | every command reached                               | the run continues once they settle                               |

Every command control flow reaches is dispatched, `OUT{}` or not; "edit, then done" means both. A
program's class is read off its walk, an earlier turn's value launching a command included. A
command is admitted at alias reservation, entered when its handler is called, settled when its
RESULT lands; one cancelled before entering is recorded. Only the fourth row refuses the message;
any other unreadable construct is dropped and named while the rest runs
([[ADR-0004-the-language#^c3|ADR-0004/C3]]).

Tag-form `OUT{}` waits for what it references, through pointers, and for no effect
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
because the model never chose it: past every live binding, values included, to a name none of them
holds. `test_a_generated_alias_a_value_holds_runs_under_a_free_suffix` pins it.

### C3: Repeats are counted, and stop is asked after the notification _(enforced: mechanical)_ ^c3

- **Subject**: every turn.
- **Violated when**: the same error on consecutive turns is told without its count, an identical
  turn is not named, or `stop()` is consulted before the turn's sections have run.

The notification says "the same error N turns running" and "your last turn was identical to the one
before it (N times now)", so the next turn has a reason to differ. An `accept` refusal is counted
like a parse error (`test_the_same_refusal_on_consecutive_turns_is_told_with_its_count`).

The sections have just looked at the world, and what they saw is what a stop decides on; asked
before, the model got one more turn after every notice that the run was over. The run ends
`Stopped(n)` with n the turn that did not run: the stopped iteration called no backend and is not a
turn.

### C4: A run ends in one of four values, and nothing else is an outcome _(enforced: mechanical)_ ^c4

- **Subject**: every run.
- **Violated when**: `run.outcome` ends outside the four, or a retry, a refusal or a fold is
  reported as one.

`Success(output)`, `Exhausted(turns, reason)` with the reason turns or time today (idle, cost and
context land with their bounds, [[ADR-0003-the-bounds#^c2|ADR-0003/C2]]), `Refused(why, times)` and
`Stopped(turn)`. `Continue` is the `Outcome` value a settled turn without `OUT{}` leaves in
`run.outcome`; a retry shows as `retry_error` and the `turn.retry` event, never as an outcome.

Open asks are cancelled when the run ends, whichever value it ends in, or an abort; a command still
pending then is cancelled and reaped, its RESULT on the record before `run` returns or raises (S4).
An exception the loop does not catch, a backend that raises, a store that cannot be written or a
command task's own defect (S5), propagates from `run` to the caller with `run.outcome` unset; the
record holds what was appended. That is an abort, not an outcome.

## Decisions

### D1: The loop is `Actor.run` as written ^d1

Serves C1 to C4. Bounds are checked at the turn's start; under `wait`, after a reply or pause,
`wait` is asked, and false ends the run, appending nothing. Then the inbox is taken, the
notification is appended, `stop` is asked, the backend is called once, the message is read, commands
are dispatched, and the turn settles. Under `wait` a turn with no command nor `OUT{}` replies, and
with `reply_on_commands` so does one whose commands all landed;
`test_with_reply_on_commands_a_turn_whose_commands_all_landed_is_the_reply_and_a_failed_one_is_not`
pins the three `wait` rows. The caller reads `run.outcome` after the loop returns.

- **Landing evidence**: `tests/test_actor.py`, `tests/test_actor_edges.py`, `tests/test_program.py`;
  `Exhausted`, `Refused`, `Stopped` compared by value, `Success` by type; the reap by
  `test_out_waits_for_what_it_names_and_the_reap_lands_the_rest_cancelled_before_run_returns`; the
  closure by `test_a_run_that_ends_by_an_outcome_closes_its_record_with_the_outcome`; program `wait`
  rows by `test_a_program_that_runs_no_command_and_gives_no_out_is_a_reply_under_wait`,
  `test_a_program_is_a_reply_under_wait_unless_its_walk_launches_a_command_or_reaches_out` and
  `test_reading_an_earlier_turns_value_that_runs_a_command_makes_the_turn_act_under_wait`;
  `_never_started` records one cancelled before entering; the clock after `wait` by
  `test_a_wait_that_outlasts_the_time_budget_admits_no_further_call`.

### D2: A retry is no outcome, so `Retry` is removed ^d2

Serves C4. `Retry` was among the exports of `lion1` and `lion1.actor` and sat in the `Outcome`
union, yet no run ever constructed one: a retry is the `turn.retry` event and `run.retry_error`
(S1). It is removed with no compatibility shim: the package is alpha and the name never carried a
value.

- **Landing evidence**: `Outcome` and the package exports no longer name it;
  `test_unreadable_turn_is_a_retry_not_an_end` goes on past the retry to `Success`.

## Alternatives

| approach                                                | rejected because                                                                                     |
| ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `OUT{}` as the only execution trigger                   | a message without `OUT{}` becomes a no-op, so the model cannot look before it decides                  |
| Commands beside `OUT{}` as scratch                      | told three times that its edit had not run, a model wrote the same message again and its fix never landed |
| `stop` asked before the notification                    | one extra model turn after every notice that the run was over                                        |
| An idle turn as the end of the run                     | the model was never asked; the idle note and `max_idle` say it instead (ADR-0003)                    |

## Consequences

- **S1**: A retry costs a turn; its message stays on the record as TEXT, and the retry itself is the
  `turn.retry` event on the actor's bus and `run.retry_error`. Its reason rides the next turn's
  notification, so a retry on the run's last turn surfaces none. A backend's refusal, a reason on
  its reply, is persisted at once in the TEXT entry's metadata; an empty reason is no refusal. A
  caller counting outcomes sees no retry. Pinned: the notification by
  `test_an_out_that_does_not_assemble_is_a_retry_that_names_what_is_wrong`, the metadata by
  `test_a_native_tool_call_beside_text_refuses_the_turn_whole_and_the_model_is_told`; no test pins
  the event.
- **S2**: The idle row keeps a talking model alive; `max_idle` (ADR-0003) is what ends it.
- **S3**: The code says turn as this corpus does: `max_turns`, `Exhausted(turns)`, `<system turn=`,
  the `turn.*` events, `Stopped(turn)`, an entry's `turn`. The earlier spelling, round, is gone with
  no alias: `max_rounds` is refused as no bound. A log written before the rename still reads, its
  entries' `round` taken as their turn. A retry is no outcome: the run has no `Retry` class.
- **S4**: The gather of every command of the turn before `OUT{}` assembled is gone: `OUT{}` waits
  for what it references, and an assembly that fails or an output refused settles the rest of the
  turn. What is pending when the run ends, by an outcome or an abort, is cancelled and reaped before
  `run` returns;
  `test_a_run_cancelled_while_out_waits_reaps_every_pending_command_and_appends_no_closure` pins the
  abort. A wait for the whole turn is at most an opt-in, later, and not built.
- **S5**: A command task that ends in neither its RESULT
  ([[ADR-0005-command-handling#^c1|ADR-0005/C1]]) nor a cancel is the runtime's own defect: the
  turn's settle raises it and the run aborts (C4):
  `test_a_command_task_ending_in_neither_a_result_nor_a_cancel_aborts_the_run`. A task no settle
  waited for (one `OUT{}` did not name) is seen through by the reap before `run` returns:
  `test_a_defect_in_a_command_out_did_not_name_is_raised_before_the_run_returns`. The abort's reap
  holds through a cancel aimed at the run (the cancelled RESULT lands, the run unregisters); a
  cancel during a defect's reap ends the run cancelled with the defect as its cause:
  `test_a_second_cancel_while_the_abort_reaps_still_lands_the_cancelled_result_and_unregisters`,
  `test_a_cancel_during_a_defects_reap_ends_the_run_cancelled_with_the_defect_as_its_cause`.
