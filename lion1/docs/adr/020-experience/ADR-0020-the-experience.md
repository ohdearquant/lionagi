---
adr: ADR-0020
status: withdrawn
liveness: retired (the runtime seam is removed; the record, the stores and the lesson's Spec are in `hub/experience.py`, wired to nothing)
date: "2026-10-02"
area: experience
kind: new
depends_on:
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0005-command-handling|ADR-0005]]"
  - "[[ADR-0006-the-notification|ADR-0006]]"
  - "[[ADR-0007-the-record|ADR-0007]]"
  - "[[ADR-0012-the-bench|ADR-0012]]"
tags:
  - adr
  - runtime
  - experience
---

# ADR-0020: Experience across runs

## Context

Withdrawn: the runtime seam is gone, and the record, stores and lesson Spec sit in
`hub/experience.py`, wired to nothing.

The model is fixed. What can move between two runs of the same ask is what the first run recorded:
the ask, how it ended, and a lesson the model wrote in its own words. Today nothing carries that. A
note outlives a run but is replaced by every put and found by substring
([[ADR-0007-the-record#^c7|ADR-0007/C7]]). The bus keeps an evictable log and persists nothing
([[ADR-0005-command-handling#^c2|ADR-0005/C2]]). The closure carries the outcome and nothing of the
ask ([[ADR-0006-the-notification#^c1|ADR-0006/C1]]).

This record fixes one seam: a recall once before the first call, a persist once after the reap, an
immutable record of each, and the two receipts on the record and the bus. It makes no claim that a
recalled lesson improves the next run; that is a bench question
([[ADR-0012-the-bench#^c8|ADR-0012/C8]]).

The port source is an earlier loop built as a graph with an evaluator node and a goal record; none
of that shape is kept. Source: `src/lion1/actor.py` (`run`, `_notify`, `_closure`),
`src/lion1/notes.py`, `src/lion1/bus.py`, `src/lion1/record.py`; `hub/experience.py` and the two
lifecycle points in `run` implement this record.

## Definitions

- **experience scope**: the exact tuple an experience record belongs to: the actor's name, the
  profile's name and a definition version, which may be none; a none is a value, never a wildcard.
- **observation**: what the runtime records when a run ends by an outcome: the ask, the outcome, the
  spend it measured and what it did not, taken after the reap.
- **lesson**: text the model wrote through `experience.lesson` for a later run of the same
  experience scope; a claim of the model's, never a fact the runtime verified.
- **experience record**: one immutable record with a full UUID: its experience scope, the source
  run, the ask, the observation, the lesson if one settled, and the ids the model claimed to have
  used.
- **experience store**: a store of experience records with two operations, `recall` and `persist`,
  and neither an update nor a delete.
- **receipt**: the canonical JSON of one recall or one persist, written on the record and copied
  into the bus event, so a later reader sees what was selected or acknowledged.
- **terminal commit**: the point after the reap at which one of the four outcomes is fixed; nothing
  done after it changes the outcome.
- **claimed use**: the record ids a lesson names as used; a claim of exposure checked against this
  run's first notification, never a proof of effect.

## Assumptions

| # | statement | source | if false |
| - | --------- | ------ | -------- |
| A1 | A lexical recall, casefolded word overlap between the ask and a stored ask plus lesson, selects a prior lesson for the same ask on the offline gate; relevance on unseen asks is unmeasured. | the gate runs one ask twice; no bench measures relevance | the store gains a ranker before any product claim; the gate stays as written |
| A2 | Some runs offer a lesson worth keeping without an extra model call; the six notes the earlier bench wrote do not show it. | ADR-0007 S3, the note count | the command is dead weight: the observation alone is kept and the lesson surface is withdrawn |
| A3 | An immutable carrier of its own costs less than changing the note store's put, delete and find. | ADR-0007 C7 and D3 fix those three as replacement, removal and substring | an experience record becomes a note kind behind a write gate, and this record is superseded |
| A4 | One second per store call is a bound, not a measured latency; the khive adapter owes its own numbers before it is bound to a profile. | no adapter exists; the bound is this record's default | the default moves with the measurement; the terminal phase's shape does not |

## Claims

### C1: Experience is opt-in per profile, and the core imports no provider _(enforced: mechanical)_ ^c1

- **Subject**: every profile and every run; `hub/experience.py` and the actor's two lifecycle
  points.
- **Violated when**: a profile with no `experience` makes a store call, offers `experience.lesson`
  or emits either event; or anything under `src/lion1/` imports `hub/` or a khive client.

`Profile.experience` is none by default, and a run of such a profile is the run of today: the same
commands, the same notifications, no event. A profile that binds an `Experience` opts into the
recall and the persist of C2 and C5, and only then is `experience.lesson` in its subset. The stores
under `src/lion1/` are in memory and on file; the khive-backed store is a hub adapter (D7).

### C2: One recall per run, against the first new INPUT, inside the experience scope _(enforced: mechanical)_ ^c2

- **Subject**: every run of an opted-in profile, queued, inputless and history-backed runs included.
- **Violated when**: a run recalls twice, recalls against an INPUT replayed from `history`, recalls
  with no first notification, or a hit from another experience scope reaches the record.

The query is the text of the first INPUT this invocation appended, after the inbox is taken and
before the first notification; a request input comes before a queued one, as today.

A run with no new non-blank INPUT records a skipped recall, never a broad search. A run that ends
before its first notification carries the skip in its closure. The store filters the exact
experience scope before it ranks, and the runtime checks each hit's scope again. A failed or
timed-out recall is a receipt with its reason; the run goes on.

### C3: What was recalled is on the record and in the model's first view, before the call _(enforced: mechanical)_ ^c3

- **Subject**: every hit a recall selects; `_notify` on the first turn and the view estimate.
- **Violated when**: a hit is kept only in metadata, a lesson is cut without the cut being counted,
  the role prompt or the generated guidance changes with a hit, or the receipt differs from the
  lines shown.

The first notification carries an `experience:` section: each hit's id, its source run and actor,
its outcome and its lesson whole, rendered as quoted data and labelled as the model's own earlier
claim, never as an instruction. The receipt rides the same entry's `meta`. Later notifications add
nothing; the first stays addressable by name ([[ADR-0007-the-record#^c1|ADR-0007/C1]]).

The block is counted in the first turn's view estimate, so the job's context check sees it
([[ADR-0006-the-notification#^c4|ADR-0006/C4]]); hits that do not fit 4,096 characters are left out
whole and counted in the receipt. This is a stated addition to
[[ADR-0006-the-notification#^c2|ADR-0006/C2]]: a value obtained outside the run, shown once.

### C4: An observation is a fact and a lesson is a claim, and neither is ever rewritten _(enforced: mechanical)_ ^c4

- **Subject**: every experience record and every experience store.
- **Violated when**: a persist replaces a record, the same id arrives with different bytes and is
  accepted, a value a store returns can be mutated into the store, or a runtime `Success` is
  presented as a grade.

A persist assigns one full UUID; the same id with the same canonical bytes is an idempotent replay,
the same id with other bytes is refused, and no operation updates or deletes. `Success` is an
accepted output, not a correct one ([[ADR-0002-the-run#^c4|ADR-0002/C4]],
[[ADR-0012-the-bench#^c1|ADR-0012/C1]]); the record keeps it as the outcome and nothing more.

The lesson is the last `experience.lesson` whose RESULT settled ok, by sequence, read from the
request as the handler entered it ([[ADR-0005-command-handling#^c5|ADR-0005/C5]]); earlier proposals
stay on the record and never reach the store. A record with no lesson is kept and is not recalled by
default.

### C5: Each terminal commit makes one persist attempt, after the reap, that cannot change the outcome _(enforced: mechanical)_ ^c5

- **Subject**: the four outcomes of an opted-in run; the phase between the reap and the closure.
- **Violated when**: a persist runs before the reap or on an abort, a store failure or timeout
  changes the outcome, a cancellation during the persist leaves the run with no closure, or a
  persist runs twice.

Once the reap returns and the outcome is set, the run makes one bounded store call, appends one
closure carrying the persist receipt, emits, delivers and completes. A refusal is `failed`; a
timeout, a lost acknowledgement or a cancellation during the call is `unknown`; neither revises the
outcome.

A cancellation in this phase is re-raised after the closure and the delivery, with the outcome kept:
it is not an abort. An abort during the model's work or the reap persists nothing, as today
([[ADR-0002-the-run#^c4|ADR-0002/C4]]). This phase is the amendment S2 names.

### C6: What the runtime did not measure stays unknown _(enforced: mechanical)_ ^c6

- **Subject**: the model, version and spend fields of every experience record.
- **Violated when**: a served model is invented from a backend that names none, a missing cost reads
  as zero, a definition version is guessed, or the lesson's text supplies provenance.

The declared model is the profile's binding or none; the record never names a served model it did
not read ([[ADR-0009-backends|ADR-0009]] S5). Spend is the sum of finite non-negative reported costs
beside a count of the calls that reported none; a run with no calls is zero, a call reporting zero
is zero, a call reporting nothing is unmetered ([[ADR-0012-the-bench#^c2|ADR-0012/C2]]).

### C7: A claimed use names only ids this run was shown _(enforced: mechanical)_ ^c7

- **Subject**: every `experience.lesson` carrying `used_ids`, and the closure that copies it.
- **Violated when**: an id not in this run's first notification is accepted, a hit from replayed
  history or one left out for size counts, or a claimed use feeds a ranker or a feedback event by
  itself.

A proposal naming an unknown id is refused whole, as the handler's result. No proposal means not
reported, not none used. The bench grader stays the only evaluator
([[ADR-0012-the-bench#^c1|ADR-0012/C1]]); a grade attached later is a new fact beside the record,
never a change to it.

### C8: The two events mirror receipts the record already holds _(enforced: mechanical)_ ^c8

- **Subject**: `memory.recalled` and `memory.persisted`.
- **Violated when**: an event is emitted before its record entry lands, its `receipt_json` differs
  from the entry's, a store write depends on a bus handler, or there is a third memory event kind.

Each event carries `schema_version`, `event_id`, `run`, `actor`, `profile`, `record_seq` and
`receipt_json`, the same string the entry's `meta` holds; a payload a handler mutates cannot reach
the record. Recording comes first, then the emit, once per operation. The bus observes and persists
nothing ([[ADR-0005-command-handling#^c2|ADR-0005/C2]]); with `keep=0` the record still holds both
receipts.

### C9: A record survives a new store instance, and a store that cannot be read is not empty _(enforced: mechanical)_ ^c9

- **Subject**: `FileExperienceStore`.
- **Violated when**: a reopened store misses a persisted record, two concurrent persists lose one, a
  malformed file reads as no hits, a failed write is served from a cache, or a retry mints a second
  id.

A persist is a locked read, check, add and atomic replace of one JSON container; an existing record
is never replaced. A read error or a parse error is raised to the runtime, which records a failed
receipt. Two processes on one file keep each other's additions.

### C10: The offline gate proves transport, not improvement _(enforced: process)_ ^c10

The example of D7 runs one ask twice and shows the second run's record, bus and first backend call
carrying the first run's lesson, and shows nothing of it when only the persist is removed. It is the
landing evidence for C2, C3, C5 and C8 and no evidence that the loop solves anything faster; that
claim waits for a paired, held-out comparison the bench record governs
([[ADR-0012-the-bench#^c8|ADR-0012/C8]]), its arms named in S4.

## Decisions

### D1: `hub/experience.py`: the record, the configuration, the lexical recall and two stores ^d1

Serves C1, C2, C4, C6 and C9. `Experience` holds the store, the declared model, the definition
version, `k` (3), `max_chars` (4,096) and `io_timeout` (1.0 s). `ExperienceRecord` is frozen, scalar
and tuple fields only, with one canonical serialization. `ExperienceStore` is a protocol with async
`recall(text, k, *, scope)` and `persist(record)`. Recall ranks by distinct query words matched over
the ask and the lesson, drops zero overlap and lesson-less records, and breaks ties by id. `__all__`
is a tuple right below the imports.

- **Landing evidence**: `tests/test_experience.py`:
  `test_a_replay_of_the_same_id_is_idempotent_and_other_bytes_are_refused`,
  `test_a_reopened_file_store_holds_every_concurrent_addition`,
  `test_scope_filters_before_k_and_a_blank_query_is_a_skip`,
  `test_a_malformed_file_raises_and_a_hit_is_a_copy`.

### D2: `Actor.run`: the recall before the first notification, the persist after the reap ^d2

Serves C2 and C5. After `_take_inbox` on turn 1 the run picks its first new INPUT and calls the
store once under `io_timeout`; the hits and the receipt are held on the run for `_notify`. After
`_reap` returns and the outcome is set, the persist runs under the same bound, then `_closure`
carries its receipt; a cancellation in that phase is re-raised after `_deliver`. Nothing of this
runs for a profile with no `experience`.

- **Landing evidence**: `tests/test_actor_experience.py`:
  `test_the_recall_reads_the_first_new_input_once_queued_and_resumed_runs_included`,
  `test_a_run_with_no_turn_records_a_skipped_recall_and_its_observation`,
  `test_each_of_the_four_outcomes_persists_after_the_reap_and_an_abort_does_not`.

### D3: `_notify`: the `experience:` section on the first turn, counted before the first call ^d3

Serves C3. On turn 1 `_notify` renders the held hits as quoted data under `experience:` after `notes
kept:`, writes the receipt into the entry's `meta["experience"]`, and includes the block in the view
estimate the context check reads. A lesson is escaped onto its lines; a hit that would cross
`max_chars` is left out whole and counted.

- **Landing evidence**: `tests/test_actor_experience.py`:
  `test_the_first_backend_call_sees_the_hits_and_the_role_prompt_is_unchanged`,
  `test_the_recall_block_is_counted_before_the_first_call`,
  `test_a_hostile_lesson_cannot_forge_a_notification_frame`.

### D4: `experience.lesson(text, used_ids=[])` as a default handler, admitted only under an opted-in profile ^d4

Serves C4 and C7. The handler checks the text (1 to 1,024 characters, not blank) and each id against
the hits this run was shown, returns the proposal as its result, and settles like any command
([[ADR-0005-command-handling#^c1|ADR-0005/C1]]). The terminal commit reads the last ok proposal by
sequence from the captured request; `OUT{}` does not wait for an unsettled one, so a proposal the
model wants kept is awaited in an earlier turn or pointed at. Its first docstring line and field
descriptions, all the guidance shows, carry these rules.

- **Landing evidence**: `tests/test_actor_experience.py`:
  `test_the_last_ok_proposal_by_sequence_is_the_lesson`,
  `test_used_ids_name_only_what_this_run_was_shown`,
  `test_a_hook_rewrite_keeps_the_entered_request_as_provenance`,
  `test_out_cancels_an_unawaited_proposal_and_nothing_of_it_persists`,
  `test_the_lesson_guidance_shows_a_whole_first_sentence_and_the_limits_the_handler_holds_it_to`.

### D5: The terminal phase: commit, persist, closure, emit, deliver ^d5

Serves C5 and C8. The order is fixed in `run`, not left to a caller's `finally`: outcome set, one
persist attempt, one closure with the receipt, the two events emitted after their entries,
`_deliver`, then any cancellation re-raised. The `except BaseException` arm keeps today's shape for
aborts before the terminal commit.

- **Landing evidence**: `tests/test_actor_experience.py`:
  `test_record_and_bus_receipts_agree_with_bus_keep_zero`,
  `test_a_refused_or_unknown_persist_keeps_the_outcome`,
  `test_a_cancel_before_the_terminal_commit_aborts_without_a_persist`,
  `test_a_cancel_during_the_persist_closes_delivers_then_re_raises`.

### D6: Spend and model fields from the run's own counts ^d6

Serves C6. Beside the `Reply` reading in `run`, the loop counts the calls that reported a cost and
the calls that did not; the record carries the sum, both counts and a `complete` flag. The model is
`Experience.model` or none; no backend is asked.

- **Landing evidence**: `tests/test_actor_experience.py`:
  `test_missing_and_partial_costs_are_unknown_and_a_reported_zero_is_known`,
  `test_an_unnamed_model_and_version_stay_none`.

### D7: The example, its mutation control, and the hub adapter as a later record ^d7

Serves C10 and C1. `examples/experience_twice.py` runs one ask twice on a scripted backend with a
`FileExperienceStore` reopened between runs; `tests/test_experience_example.py` reads the second
run's record, bus and first backend call, then repeats with the store write removed and with the
file deleted. `hub/experience.py` (`KhiveExperienceStore` over `src/lion1/khive.py`) lands under its
own record once it proves scope filtering before `k` and idempotency over an ambiguous write.

- **Landing evidence**: `tests/test_experience_example.py`:
  `test_the_second_run_recalls_the_first_runs_lesson_on_record_bus_and_call`,
  `test_removing_only_the_persist_recalls_nothing`,
  `test_a_reopened_empty_store_recalls_nothing_from_the_actors_memory`.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| The earlier loop's shape: a graph with an evaluator node and a goal record | lion1 has turns, four outcomes and `queue`; a graph and an evaluator are two new layers, and a goal record repeats the INPUT (ADR-0002, ADR-0010) |
| Recall at every command settlement, hand-off or turn | a settlement is not an outcome, a hand-off repeats the peer's own recall, and a per-turn block grows an append-only view by the block each turn; none has a measurement (C10) |
| The runtime writes the lesson from the outcome | "ask: Success" carries no technique; the observation is kept as a fact and the lesson stays the model's (C4) |
| A key convention over the note store | a put replaces, a delete removes and `find` is substring (ADR-0007 C7); fencing those for one caller changes the store's contract (A3) |
| The bus alone as the carrier | its log evicts and can be off, and a subscriber that persists races the next run's recall (C8) |
| A fifth record kind for the receipts | every reader of the record changes; a SYSTEM entry's `meta` carries them (ADR-0007 C1) |
| The khive store as the default | the core stays provider-free and the gate runs offline; the adapter is a hub record of its own (D7) |

## Consequences

- **S1**: D1 to D7 landed with the tests they name. C1 is also pinned by
  `test_a_profile_without_experience_runs_as_it_did_and_offers_no_lesson` and
  `test_nothing_under_lionagi_imports_hub_or_a_khive_client`, and C2's failed recall by
  `test_a_failed_recall_is_a_receipt_and_the_run_goes_on`. Each of these turned a named test red:
  deleting the file store's write, the store's scope filter, the block's count in the view estimate
  or the cancel's catch at the store call, and emitting the recall event before its entry. C9's two
  writers are threads with a store each over one file, not two processes.
- **S2**: The terminal phase amends [[ADR-0002-the-run#^c4|ADR-0002/C4]] and
  [[ADR-0006-the-notification#^c1|ADR-0006/C1]]: after the reap a cancellation no longer aborts the
  run; it is re-raised after the closure and the delivery with the outcome kept. ADR-0006/C2 gains
  the `experience:` lines. Both amendments land with D5 and D3, in the same change.
- **S3**: The store and the record are two failure domains: a process killed between the store's
  acknowledgement and the closure leaves a stored record whose run has no closure. Nothing promises
  exactly-once delivery or recovery across a kill; a replay of the same id is the only idempotency.
  A cancel during the store call leaves the closure written, its receipt `unknown`, and the answer
  delivered, then is re-raised; neither step awaits, so no cancel lands between them, and a slow
  store is not waited for (`test_a_cancel_during_the_persist_closes_delivers_then_re_raises`).
- **S4**: The product claim, that the same model inside the loop solves a task class faster and more
  often than the same model alone, is not made here. It needs paired, held-out trials on frozen
  stores with the same served model, tools and budgets, and an external grade. The arms are four:
  alone, an empty store, observations only, lessons; exact-task repetition and task-class transfer
  are reported apart ([[ADR-0012-the-bench#^c8|ADR-0012/C8]]).
- **S5**: Exact scope matching gives up reuse across profiles and versions; lexical recall misses
  paraphrase; recall at the start misses an ask that changes mid-run, a continuous run's later wakes
  above all ([[ADR-0013-the-agent#^c7|ADR-0013/C7]]). Each is a later record, not a hidden cost of
  this one.
- **S6**: A hostile lesson is shown as quoted data under a label; the label is a frame, not a proof
  that a model will not follow it. The model's use of a lesson is the model's claim (C7).
- **S7**: The boundary: a durable experience store shared across processes, its retention, and who
  may read whose records are the kernel's. Here the store is memory or one file per directory, and
  the khive adapter is the in-process client of that boundary.
- **S8**: The desk's `record` lookup gains no memory retrieval ([[ADR-0017-the-desk|ADR-0017]] S9);
  `Record` reads no memory.
