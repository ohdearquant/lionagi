---
adr: ADR-0007
status: draft
liveness: operating (the note read pinned per command, the captures on the entry, the consumed directive, the fold by date and the cross-profile note read are owed)
date: "2026-09-23"
area: record
kind: new
depends_on:
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0006-the-notification|ADR-0006]]"
tags:
  - adr
  - runtime
---

# ADR-0007: The record, the view and the notes

## Context

A run needs one history that is never edited, a view of that history small enough for the model to
hold, and a place where the model keeps what it learns across runs. Each of the three has a cost
when it is missing: a value cut short is re-fetched, a view folded every turn breaks the provider's
cache, and a model with no notes starts every run from nothing. This record fixes the three.

The record belongs to the run ([[ADR-0002-the-run|ADR-0002]]); the notification that reads it each
turn is [[ADR-0006-the-notification|ADR-0006]]; the context and note commands are dispatched like
any command ([[ADR-0005-command-handling|ADR-0005]]). Source: `lionagi/record.py`, `fold`,
`Context.direct` and the default handlers in `lionagi/actor.py`, `lionagi/notes.py`.

## Definitions

- **record**: the run's append-only history: a sequence of entries of four kinds (C1).
- **entry**: one item on the record, with a sequence number and a name derived from it.
- **view**: what the model sees of the record: every entry in order, some hidden or summarised.
- **directive**: an instruction to the view, stored on the entry that made it: hide, show, or
  summarise named entries.
- **fold**: the pass that applies the directives in record order and renders the view.
- **fold event**: the runtime's own hide directive, made when the view crosses `view_budget`.

- **placeholder**: the line a hidden entry renders as: its name and size, with a RESULT's alias and
  Spec or another entry's kind.
- **`view_budget`**: the view size past which the runtime folds by itself (ADR-0003).
- **context commands**: `context.hide(refs)`, `context.show(refs)`, `context.summarize(refs, text)`;
  default handlers of every actor.
- **note**: a value written under a `note.` key that outlives the run, per profile.
- **note store**: the notes' carrier: the store a profile brings, else memory for the actor's
  lifetime, or one JSON file per profile under `notes_dir`.
- **note commands**: `note.list(prefix)`, `note.find(query)`, `note.get(key)`, `note.delete(key)`;
  default handlers of every actor.

## Assumptions

| #  | statement                                                                                                   | source                                                                                                                  | if false                                        |
| -- | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| A1 | An append-only view keeps the provider's prefix cache warm; a fold moves the prefix, so traffic saved is not spend saved. | on the bench: cache hit 90.6% through the loop; uncached input 1 to 2k tokens per call; an outside study where a condensed run cost more | C2's event should fold earlier, or never |
| A2 | Hiding an old result behind a placeholder costs no success.                                                | SWE-agent on SWE-bench Verified: raw 53.4% at $1.29, masked 54.8% at $0.61, summarized 53.8% at $0.64 per trajectory | the fold event owes a summary, and a model call to write it |
| A3 | One JSON file per profile is enough for the note volumes an actor writes.                                  | assumption; a 50-instance bench run wrote 6 notes in 952 turns                                                          | C5's carrier is replaced, the interface kept    |
| A4 | The model curates its own view once a task is long enough to need it. | assumption; 9 context commands in 952 turns of short bench tasks (S3) | the runtime owes a summarizer of its own |

## Claims

### C1: Four kinds, append-only, names are pointers _(enforced: mechanical)_ ^c1

- **Subject**: every record.
- **Violated when**: an entry is changed or removed, a record name or a RESULT alias resolves to a
  different entry over time, or an alias starting with `_` is admitted.

| kind   | who wrote it | what it holds                                   |
| ------ | ------------ | ----------------------------------------------- |
| INPUT | the caller | the job's inputs, and anything queued from elsewhere |
| SYSTEM | the runtime | one notification per turn, and the closure at the end (ADR-0006/C1) |
| TEXT | the model | the raw message, LNDL included |
| RESULT | the runtime | a settled command's value or its failure, under its alias |

Every entry has a monotonic sequence and a name derived from it: `_in3`, `_sys12`, `_m40`, `_r41`. A
RESULT also carries the model's alias, registered once for the run. `get` resolves either name;
names live as long as the record, in view or not. The leading underscore is the record's, so the
model's names and the record's never meet.

A value's alias names no entry: a later admitted declaration replaces the value and its binding
([[ADR-0002-the-run#^c2|ADR-0002/C2]]). An entry is frozen and its content captured: an immutable
value shared, a list or dict copied and frozen, a Spec its stored fields, so nothing that runs later
changes what it holds. The handler's return and the request it received ride `meta` as provenance no
pointer reaches (ADR-0005/C5).

### C2: The view is append-only; the fold is an event _(enforced: mechanical)_ ^c2

- **Subject**: every render of the view.
- **Violated when**: an entry leaves the view without a directive, a fold event fires below the
  budget or twice per crossing, a directive crashes the fold, or a value is truncated.

Every entry stays in view where it landed: one render adds to the last and the provider's prefix
holds. INPUT is never hidden (`fold_inputs` excepted, [[ADR-0009-backends#^c3|ADR-0009/C3]]).
Directives apply in record order. A hidden entry renders as a placeholder, one summarised as its
summary. A value is never truncated: it stays whole on the record, a pointer away.

Past `view_budget` (60,000 tokens, ADR-0009/C3) the runtime hides the oldest RESULTs the model has
seen until the view is near half the budget, by one directive on the notification per crossing. With
none left the fold stops short (ADR-0003/C2). A RESULT a program consumed is hidden the same way, by
a directive dated at the consumption ([[ADR-0008-the-program#^c5|ADR-0008/C5]]): the one value
hidden unseen.

### C3: The context commands move the view and never the record _(enforced: mechanical)_ ^c3

- **Subject**: `context.hide(refs)`, `context.show(refs)`, `context.summarize(refs, text)`.
- **Violated when**: a command removes or changes an entry, a directive lands anywhere but the
  command's RESULT or the runtime's notification or closure, the fold applies directives out of date
  order, or a hidden entry fails to dereference.

The handler calls `Context.direct`; the directive rides the command's RESULT, dated at its sequence.
The runtime's ride the notification or the closure, dated at their event. The fold applies
directives by date (today record order, C2), so a `context.show` after a consumption stands over its
hide, and a directive reaches only entries earlier than its date.

A directive pins its targets' sequences when made; the fold reads those, never the names. A summary
stands where the span began and keeps the names it replaced. Hiding changes the rendering only;
`*name` and `context.show` still reach the entry.

A hide applies when asked; the guidance batches hides at a phase boundary, since each moves the
provider's prefix.

### C4: A resumed chat folds the same directives over the same record _(enforced: mechanical)_ ^c4

- **Subject**: every `lion chat -c` / `-r` over a logged conversation.
- **Violated when**: the first view of the resumed session differs from the view at the end of the
  logged one.

A chat's record is logged entry by entry and read back as the next session's history, directives
included, so the fold lands where it did. Each logged turn's values are folded in order by the live
loop's admission rule; no note is written and no command runs.

The guarantee is over the same fold; a session whose renderer changed is not detected
([[ADR-0018-chat-in-a-box#^c6|ADR-0018/C6]]). That is chat history, not a run checkpoint: a run
ended mid-turn does not resume, its unsettled commands are named and not rerun.

### C5: A `note.` value is in the store after the turn, per profile, and told _(enforced: mechanical)_ ^c5

- **Subject**: every `<lvar>` whose alias starts with `note.`, and every profile's Operable.
- **Violated when**: a `note.` value is not in the store after its declaration, short of a later
  admitted write or delete, `notes written` omits it, two profiles share a store they did not ask
  for, or a blank `note.find` matches.

In the tag form a value is written at declaration, once the message is read and before the turn's
commands run; in a program a note assignment is a node, written when reached
([[ADR-0008-the-program#^c4|ADR-0008/C4]]). `*note.key` resolves to the value this run declared,
else to the store, read once per command with its version; `note.get` always reads the store. A note
carries the run and sequence that wrote it and its version. The carrier is memory, or with
`notes_dir` one JSON file per profile.

`note.list` returns key, size, run and version for an exact prefix; `note.find` refuses a blank
query. Reading another profile's notes is not built.

### C6: A context command names only entries on the record _(enforced: mechanical)_ ^c6

- **Subject**: the refs of every `context.hide`, `context.show` and `context.summarize`.
- **Violated when**: a context command naming anything not on the record stores a directive or
  settles ok.

One ref that names no entry, a result of the same turn not yet landed included, fails the whole
command with the names it could not find, and no directive is stored, so none stands that hides
nothing. The fold skips a name that resolves to nothing, which a directive read back from a log row
without `seqs` can carry, so no directive crashes it; no test pins the skip.

### C7: A note is kept as given, and found without regard to case _(enforced: mechanical)_ ^c7

- **Subject**: every put to a note store, and every `note.find`.
- **Violated when**: an overwrite removes a key, the file store writes or coerces a value JSON
  cannot carry, or `note.find` misses a note whose key or value holds the query in another case.

A put replaces the value whole and raises the note's version, an empty value included, so a key
leaves the store only by a delete, `note.delete` for the model. The file store refuses a value JSON
cannot carry before it writes anything. `note.find` returns each hit's first 120 characters as its
preview, with its size, run and version. A store the profile brings (`Profile.notes`) replaces the
actor's for every run of that profile. No test pins the empty overwrite, the 120-character cut or a
brought store.

## Decisions

### D1: `record.py` and `fold` ^d1

Serves C1 to C4. The record is a list of frozen entries with a sequence counter. `fold` walks it
once, applying each directive at its sequence, and returns view entries with a hidden flag and the
summary text; `_notify` calls it once per turn for the size, the out-of-view diff and the fold event
(ADR-0006).

- **Landing evidence**: `tests/test_actor.py` (the fold event, the placeholder, the summary, the
  context figure across a fold), `tests/test_chat.py` (resume by replaying the chat log).

### D2: Directives ride the entry that made them ^d2

Serves C3, C4 and C6. The RESULT for a command; the notification for the fold event and the consumed
marks, the closure for the marks of a last turn; no separate directive log, because each carries its
date and a resumed chat replays them by folding. `_perform` pins the targets' sequences as it stores
a command's directive, and stores none when a ref names nothing on the record.

- **Landing evidence**: `test_a_hidden_result_is_a_placeholder_and_a_summarized_one_is_not`,
  `test_a_directive_naming_nothing_on_the_record_is_refused_not_stored_empty`.

### D3: `notes.py` with a memory store and a file store ^d3

Serves C5. `FileNoteStore` subclasses `MemoryNoteStore`; a put re-reads the file, replaces the whole
value and rewrites the file atomically, under a lock every process on the directory shares. Two runs
writing one key: the later put wins, and `version` says a write happened, never which was lost. The
four note Specs and the three context Specs are the default handlers every actor gets. Every write
is a put, from the `note.` declaration or a program's assignment, or `note.delete`'s delete; a gate
on writing, when decided, sits on those two.

- **Landing evidence**: `tests/test_notes.py`; the bench records carry the live writes.

### D4: The stores take a value as given, and `_note_find` cuts the preview ^d4

Serves C7. `put` on either store keeps the value it is handed; the file store first tries
`json.dumps` on it and raises `ValueError` before it takes its lock. `_note_find` slices the value's
text at 120 characters, and `Actor.notes_for` returns `Profile.notes` before it looks for a store of
its own.

- **Landing evidence**: `test_a_file_note_store_refuses_a_value_the_file_cannot_carry` and
  `test_a_blank_find_is_refused_and_a_letter_query_is_case_insensitive_while_list_prefix_is_exact`.

## Alternatives

| approach                                          | rejected because                                                                                  |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| A fold event every turn | the prefix moves every turn and the cache is lost (A1) |
| Truncate big values                               | hidden caps forced turns and wrong fixes that were blamed on the model                          |
| A separate directive log                          | the record already orders directives; a second order would have to be reconciled on resume        |
| A summary written by a model call at each fold    | costs a call per fold; a placeholder with name and size loses no success (A2)                     |
| File-backed notes by default                      | a test actor should not touch disk; the actor opts in with `notes_dir`                            |

## Consequences

- **S1**: One render differs from the last by what was appended, so the provider's prefix holds
  until a directive moves it.
- **S2**: A run checkpoint exists only for the long-running agent's continuous run, replayed as
  history ([[ADR-0013-the-agent#^c7|ADR-0013/C7]]); any other run keeps only its note store and does
  not resume; a chat does.
- **S3**: The context layer is barely exercised on short tasks (109 `<lvar>`, 6 notes, 9 context
  commands in 952 bench turns); long tasks are where it is measured.
- **S4**: A `note.` declaration is a write no hook and no privilege gates: the store is the
  profile's own, and a profile whose subset omits the note commands still writes when the model
  declares one. A run that must read the profile's notes and write none, an evaluation against a
  fixed baseline, has no answer today. Every write path is a put or a delete (D3), so a read-only
  view of the same store, same names and versions, refusing at both, is the shape; it needs no new
  privilege.
- **S5**: A single RESULT larger than the view budget has no rule: it renders whole, the fold never
  hides what the model has not seen, and the backend's own limit is what ends the run. A sized
  preview with the value kept whole is proposed in review and not decided.
- **S6**: Reading another profile's notes is decided as its own default command, naming the profile
  and gated by a privilege ([[ADR-0001-the-actor#^c4|ADR-0001/C4]]); it is owed, with a test that a
  profile lacking the privilege is refused.
- **S7**: A fold event makes the provider re-read, once, what stays in view after the first entry it
  hides, and spares every later turn the hidden tokens. Folding to half the budget keeps that
  re-read no larger than about what was hidden, so the event pays for itself within about r turns, r
  being the provider's price for re-read input over its price for cached input.
- **S8**: Hidden entries in a row render as one placeholder line naming each of them; no test pins
  the grouping.
