---
adr: ADR-0006
status: draft
liveness: operating (the closure entry is owed)

date: "2026-09-23"
area: notification
kind: new
depends_on:
  - "[[ADR-0003-the-bounds|ADR-0003]]"
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0005-command-handling|ADR-0005]]"
tags:
  - adr
  - runtime
---

# ADR-0006: The notification

## Context

With nothing awaited inline, the model needs one place to learn what settled, what was refused and
why, what it can no longer see, how much context it is using, and the state of the thing it is
working on. Without that place a model re-reads results, repeats a wrong message, and declares done
on an unchanged tree. This record fixes that place: one notification per turn, its contents, and who
may write into it.

Once per turn, before the backend call, the runtime appends one SYSTEM entry to the record
([[ADR-0007-the-record|ADR-0007]]). The turn is [[ADR-0002-the-run|ADR-0002]]; the bounds the entry
reports are [[ADR-0003-the-bounds|ADR-0003]]; the hook failures it lists come from
[[ADR-0005-command-handling|ADR-0005]]. Source: `Actor._notify` and `Actor.section` in
`lionagi/actor.py`.

## Definitions

- **notification**: the one SYSTEM entry the runtime appends each turn, before the backend call.
- **closure**: the one SYSTEM entry the runtime appends when the run ends by an outcome, after the
  cleanup; no backend in this run reads it.
- **frame**: the notification's envelope, `<system round=n/N time=elapsed[/budget] seq=k>` to
  `</system>`.
- **section**: a function registered with `actor.section`; a contributor to the notification.
- **view estimate**: the view's size at four characters per token.
- **context figure**: the runtime's number for what the model's context holds: anchored on the last
  prompt count a backend reported, grown by the estimate since.

## Assumptions

| #  | statement                                                                             | source                                                                                            | if false                                   |
| -- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| A1 | A settled value is read once, in full, in its RESULT; the notification only names it. | the view keeps a new RESULT in full for the turn it arrives (ADR-0007)                          | C2 must carry values, and the view doubles |
| A2 | The state of the thing being worked on is what a long run needs most from the system. | on the bench, the watch's line turned settled-wrong misses into resolved runs ([[ADR-0011-the-box|ADR-0011]] and [[ADR-0012-the-bench|ADR-0012]], the box and the bench) | C3's sections are decoration               |
| A3 | Four characters per token is a good enough estimate for what the view added since the last call. | measured on 4,240 bench calls: the provider's prompt count read 1.20 times the view estimate plus 1,695, so the anchored figure's median error is 0.7% against 24.6% unanchored | C4 needs a tokenizer |

## Claims

### C1: One SYSTEM entry per turn, numbered by its sequence _(enforced: mechanical)_ ^c1

- **Subject**: every turn.
- **Violated when**: a turn starts without a fresh notification, two are appended in one turn, or a
  run ends by an outcome without its closure.

The frame is `<system round=n/N time=elapsed[/budget] seq=k>`, closed by `</system>`. The entry's
record name (`_sys12`) lets an old notification be pointed at after it has left the view. A model
that writes the frame itself is refused whole ([[ADR-0004-the-language#^c4|ADR-0004/C4]]).

A run that ends by an outcome appends the closure after its last turn and the reap: it carries the
outcome, the cancelled aliases and the directives no notification follows
([[ADR-0008-the-program#^d3|ADR-0008/D3]]). It is not a turn and calls no backend; an abort appends
none.

### C2: The contents are what the runtime knows and the model does not _(enforced: mechanical)_ ^c2

- **Subject**: every notification.
- **Violated when**: a settled command is missing, a value is repeated in full, a refusal carries no
  reason, or a line outside this list appears.

In order:

- `context:` C4's figure against `context_budget` and the fold line `view_budget`; each bound's
  remainder;
- "your last message could not be read", the error, its repeat count, a repair's note
  ([[ADR-0004-the-language#^c3|ADR-0004/C3]]);
- the idle note, and the identical-message note;
- `settled:` with alias, record name, Spec, type and preview;
- `failed after starting:` and `completion unknown:`, by the RESULT's `execution` mark
  (ADR-0005/C1);

- `out of view now`, what left the view since the last render;
- `folded:`, the fold event, the span hidden and its size (ADR-0007);
- `not executed:` with alias and reason;
- `notes written:`;
- `hooks failed`, after hooks that raised (ADR-0005/C5);
- `inbound:` with origin and handle;
- each section's text, or `section failed:` with the exception.

Per-turn lists clear once told.

### C3: Every section runs every turn, before stop is asked, and writes only here _(enforced: mechanical)_ ^c3

- **Subject**: every function registered with `actor.section`.
- **Violated when**: a section is skipped in a turn, or its text lands anywhere but the
  notification.

A section is called every turn and awaited when it must look somewhere. It is where the system says
the state of the thing being worked on, not only the state of the conversation. Sections run before
`stop` is asked ([[ADR-0002-the-run#^c3|ADR-0002/C3]]), so what they saw can end the run. A section
that raises is told as `section failed:` and the turn goes on.

### C4: The context figure anchors on the prompt count a backend reports _(enforced: mechanical)_ ^c4

- **Subject**: the `context:` line of every notification.
- **Violated when**: a reported prompt count is ignored, or the figure is the bare view estimate
  while a count for this run exists.

A backend may hand back a `Reply` carrying the provider's prompt count for the call (fresh input,
cache read and cache write together; [[ADR-0009-backends#^c2|ADR-0009/C2]]). The runtime pairs that
count with the view estimate it sent. While the view grows, the figure is the last count plus the
estimated growth since.

When the view shrinks (a fold or a `context.hide`), the figure takes off what left at the run's
measured rate, the reported growth over the estimated growth summed across the run's calls, and
never below the estimate. A backend that reports nothing leaves the figure at the view estimate; the
fold measures the estimate plus the reported context above the run's floor
([[ADR-0009-backends#^c3|ADR-0009/C3]]).

## Decisions

### D1: `_notify` builds the notification and clears the per-turn lists ^d1

Serves C1 to C4. The view is folded once for the size estimate and the out-of-view diff, then the
lines are joined; the context line is written after the fold has settled the view; `settled`,
`refused`, `failed`, `unknown`, `notes_written` and `hooks_failed` are emptied in the same call. The
entry is on the record before the backend is called, so a backend that raises ends the run with the
diagnostics on the record (ADR-0002/C4).

- **Landing evidence**: `tests/test_actor.py`; `test_an_awaited_section_lands_in_every_notification`
  pins C3; the anchored-figure and fold-rate tests pin C4 against a stand-in backend whose count
  follows a law the estimate does not know.

## Alternatives

| approach                                     | rejected because                                                                              |
| -------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Repeating settled values in the notification | doubles the view; the RESULT already carries the value and a pointer reaches it later         |
| Sections as extra user messages              | one frame per turn is what the model learns to read; scattered messages compete with results |
| A tokenizer for the context figure           | a dependency per provider for a figure that is a reference for the model; the anchor is exact where it matters and estimates only the delta |
| Scaling the count by the view's ratio on a shrink | it also scales a backend's fixed context, which never shrinks: up to 96% off at a fold in simulation, 5% by the measured rate |

## Consequences

- **S1**: A name is told once when it leaves the view, and the model can bring it back by name.
- **S2**: The notification is the one place a section can steer the model; the bench's settle and
  criteria lines live there ([[ADR-0012-the-bench|ADR-0012]], the bench).
- **S3**: The figure is an estimate for one turn after a fold on a backend that keeps its own
  conversation, which starts over from the folded view ([[ADR-0009-backends#^c4|ADR-0009/C4]]); the
  next reported count corrects it.
- **S4**: A section has no timeout: one that hangs holds the turn, where a hook is cut at its
  `timeout` (ADR-0005/A4).
- **S5**: The `still running:` line cannot appear while a turn gathers its commands before the next
  notification (ADR-0005/C2); it stays until the turn's wait is decided (ADR-0002/S4).
