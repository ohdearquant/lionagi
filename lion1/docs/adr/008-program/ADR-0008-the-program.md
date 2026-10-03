---
adr: ADR-0008
status: draft
liveness: operating (the consumed marks and computed values of a last turn on the closure, a replay of values nothing read, the normalised program on the TEXT entry, the tag form lowered to the same nodes and the name repairs with their bench measurement are owed)
date: "2026-09-23"
area: program
kind: extends
depends_on:
  - "[[ADR-0004-the-language|ADR-0004]]"
  - "[[ADR-0005-command-handling|ADR-0005]]"
  - "[[ADR-0007-the-record|ADR-0007]]"
tags:
  - adr
  - runtime
  - lndl
---

# ADR-0008: The program

## Context

A command whose result the model must read costs a turn today: the model asks, the result lands in
its view, the model reads it and asks again. Finding books similar to one and keeping those over a
threshold takes two model calls, and the candidate list passes through the context twice, as a
result and retyped into the output. A script does it in one pass and hands back only what was asked
for.

The dispatcher is already a dataflow executor: one task per command, a reference to another command
waits for it to settle, a pointer is dereferenced at execution time, and `OUT{}` waits for what it
names ([[ADR-0005-command-handling#^c1|ADR-0005/C1]]).

Missing above it: expressions over values, control flow, a result written to a note instead of the
view, and a reading that fixes small slips without executing a message it could not read.

The grammar exists as a parser with no evaluator and no consumer: the symbolic phase of the `krons`
LNDL package (lexer, Pratt parser, AST with `if`/`elif`/`else` by indent, inline `if`, `return`,
`note["key"]`, `DO`). This record adopts it as the program form, compiled onto the existing
dispatcher. Source: `lionagi/lndl.py`, `Actor._dispatch` and `_perform` in `lionagi/actor.py`,
`krons/lndl/symbolic_*.py` and `krons/lndl/fuzzy.py`.

## Definitions

- **program form**: the message written as a fenced block of statements, blocks by indent; a profile
  option beside the tag form.
- **graph**: what a message compiles to: nodes for commands, values, conditionals and the output;
  edges for the references between them.
- **node**: one unit the dispatcher runs: a command, a value, a conditional holding sub-graphs, or
  the output.
- **control result**: what a block finishes with: Continue, Return(value) or Failed; only Continue
  runs the block's lexical suffix.
- **expression**: a computation over values, evaluated by the runtime when its node executes (C3).
- **repair catalogue**: the closed list of normalisations applied before compilation (C2).
- **consumed result**: a RESULT whose only readers are other nodes or a note assignment, and that
  `OUT{}` does not name.
- **`language`**: the profile's choice, `core` (the tag form) or `program`.

## Assumptions

| #  | statement                                                                                          | source                                                                                        | if false                                                   |
| -- | -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| A1 | Most unreadable messages are small slips with one obvious repair, not wrong programs.                 | 55 recorded unreadable messages: closers, commas, quotes, line breaks; 5 left after four readings | C2's repair catalogue buys little; the tax is elsewhere    |
| A2 | A model that can write a two-line shell pipeline can write a two-line program in this form.        | assumption; bench messages already chain `*a` into a second command in one turn                 | the program form stays a per-profile option (C6)           |
| A3 | Intermediate results the model never reads cost context and buy nothing.                          | on the bench the view held every RESULT for a turn whether or not the model used it          | C5's curation saves less than the view budget already does |

## Claims

### C1: A message is compiled whole, then run; one that does not compile runs nothing _(enforced: mechanical)_ ^c1

- **Subject**: every message under the program form.
- **Violated when**: a node executes before the whole message compiled, or a message with a syntax
  error executes in part.

The message is read into the graph first. The graph is checked before anything runs: every command
name is in the profile's subset, every reference names a node or a record entry, no cycle over
reference, control and effect edges together, every expression well-formed, no alias collision. A
failed check refuses the message as one retry, naming the line and the reason
([[ADR-0002-the-run#^c1|ADR-0002/C1]]); nothing ran, so nothing is half done.

Whole-message admission is the decision: nothing runs until every line is read and checked, so a
syntax error never leaves the world changed. A graph, not a tree walk, because the dispatcher
schedules by dependency and each node needs an identity. A node that fails at execution leaves the
settled nodes settled: the check refuses, it does not roll back.

### C2: Fuzzy reading is a normalisation pass with a closed catalogue, and every repair is named _(enforced: mechanical)_ ^c2

- **Subject**: every message before compilation.
- **Violated when**: a repair outside the catalogue is applied, an ambiguous repair is applied, a
  repair is applied silently, or a repaired message that still does not compile runs.

Before compilation the reader applies a fixed list of repairs, each recorded: missing closers, a
missing comma, line breaks inside a string, a swallowed closing tag, invented tag shapes (`{lact
x}`, `Note.`), whitespace or case in an `OUT{}` key. Also a command, field or alias name matched to
one known name by string similarity: Jaro-Winkler, a threshold per kind; two candidates within 0.05
is ambiguous.

A repair applies only when it is unique and the repaired message compiles; an ambiguous one refuses
the message naming the candidates. The name repairs start off, because the nearest known name is not
the meant one when that is absent from the subset; D2's measurement turns each on by kind. Every
applied repair rides the next notification ([[ADR-0006-the-notification#^c2|ADR-0006/C2]]).

### C3: Expressions over values are evaluated at execution, not by the model _(enforced: mechanical)_ ^c3

- **Subject**: every argument, assignment and `OUT{}` field under the program form.
- **Violated when**: an expression is evaluated by anything but the runtime's own evaluator, or can
  call anything that is not a command node.

The expression language is the symbolic grammar's: a pointer `*a`, field access `.title`, index
`[0]`, `+ - * /`, comparisons, `and`, `or`, `not`, literals, `note["key"]`, an inline `a if c else
b`. One comprehension is added, `(x for x in *r if x.score > 0.8)`.

The runtime's evaluator (never `eval`) computes it when its node executes; a pending reference
waits. Values are data (scalars, strings, lists, dicts, Spec fields); access and indexing never
call. A type error settles the node `invalid`, naming both. The only loop is the comprehension,
whose name a `DO` inside it reads; a call is a command node, `DO name(...)` (C6). `note["key"]` is
read once per node: one expression, one version. A build past 100,000 characters and items settles
`invalid`.

### C4: A command's result may be assigned to a note, and then it is not in the view _(enforced: mechanical)_ ^c4

- **Subject**: every assignment `note.key = ...` and `note.key.path = ...`.
- **Violated when**: a note assignment renders its value in the view, a note is written before its
  value settled or in a block not reached, or a failed result is written as a note's value.

A note assignment is a node: `note.book1 = {...}` writes when its block reaches it, never hoisted
past a branch or `return` ([[ADR-0007-the-record#^c5|ADR-0007/C5]]); two assignments from one
producer are two writes. `note.book1.similar = search(...)` runs the command; at settle its result
is written there (a dotted path writes into a JSON note) and the RESULT renders as a placeholder,
name and size, never in full. The record keeps the value; the pointer and `context.show` reach it.

A producer that failed, was refused or cancelled writes nothing: the note keeps its value and the
assignment is told not written; a store failing after the producer settled keeps the RESULT and
claims no write.

### C5: The view is curated: consumed intermediates render as placeholders _(enforced: mechanical)_ ^c5

- **Subject**: every RESULT produced by a program.
- **Violated when**: a result consumed only by other nodes or by a note renders in full, or a result
  the program left unconsumed is hidden.

A RESULT that another node consumed, or that a note assignment took, renders as a placeholder in the
turn it lands; a RESULT nothing consumed renders in full as today, and so does what `OUT{}` names.
The model asks to see a consumed value by pointing at it or by `context.show`. This is a rendering
rule, not a record rule: the record holds every value whole
([[ADR-0007-the-record#^c2|ADR-0007/C2]]).

### C6: The program form is a profile option; the three constructs stay the core _(enforced: mechanical)_ ^c6

- **Subject**: every profile.
- **Violated when**: a profile without the option is shown program guidance, or a program message is
  accepted from it.

A program is written in a fenced block, statements by line, blocks by indent:

```
note["book1"] = {title: "…", description: "…", author: "…"}
similar = search_similar(input=*note["book1"].title + " " + *note["book1"].description, candidates=*catalog)
chosen = *similar if *similar != [] else DO search_similar(input=*note["book1"].title, candidates=*catalog)
note["book1"]["similar"] = *chosen
OUT{similar_books: (b for b in *chosen if b.similarity > 0.8)}
```

Prose outside the block is scratch; inside, a `#` line is a comment. A command runs when its block
reaches it ([[ADR-0002-the-run#^c1|ADR-0002/C1]]); one inside an `if` only when its branch is taken;
`return` ends the program with a value for `OUT{}` (C7). A name bound in a branch stays there; one
that escapes is bound outside by the inline `if`. Inside, what is written runs or is refused; a
value costs nothing until dereferenced.

The tag form of [[ADR-0004-the-language#^c1|ADR-0004/C1]] compiles to the same graph under its own
admission: an unknown name or a taken alias refuses that command, not the message
([[ADR-0005-command-handling#^c1|ADR-0005/C1]]); C1's check is the program's. A profile names its
language, `core` (default) or `program`, and sees only its guidance.

### C7: A block finishes with a control result, and only Continue runs its suffix _(enforced: mechanical)_ ^c7

- **Subject**: every block of a program: the top level and each branch.
- **Violated when**: a node runs after its block returned or failed, an untaken branch runs, or a
  refused `return` falls into the suffix it skipped.

Continue runs what follows in the block; Return(value) hands `OUT{}` one candidate and ends the
program through every enclosing block; Failed likewise. A conditional's control result gates every
later node of its block, and a taken `return` inside it propagates outward, so a nested suffix is
gated as the top level is. A refused candidate is a retry ([[ADR-0003-the-bounds#^c4|ADR-0003/C4]]),
never a fall into the skipped suffix. A forward pointer that crosses its own `return` guard is a
cycle over reference and control edges and refuses the message (C1).

## Decisions

### D1: A compiler in `lionagi/lndl/`, its graph handed to the existing dispatcher ^d1

Serves C1, C3, C6 and C7. `read` normalises (C2), the parser builds the AST, and `compile` lowers it
to the graph, an `if` becoming a conditional node whose control result gates its suffix (C7).
`check` validates it over reference, control and effect edges, and the nodes are dispatched as
commands are, the evaluator called at dereference. The consumed mark is a directive dated at the
consumption, on the next notification or the closure ([[ADR-0007-the-record#^d2|ADR-0007/D2]]), so a
resumed chat folds it and recompiles nothing; the record keeps four kinds
([[ADR-0007-the-record#^c1|ADR-0007/C1]]).

- **Landing evidence**: `lionagi/program.py` (beside `lionagi/lndl.py`, which a `lionagi/lndl/`
  package would shadow); `tests/test_program.py`: `test_the_records_example_resolves_in_one_turn`,
  `test_a_syntax_error_refuses_the_whole_message_and_nothing_runs`,
  `test_a_check_that_fails_refuses_the_whole_message_naming_the_line`,
  `test_a_build_past_the_size_limit_settles_the_node_invalid`,
  `test_a_comprehension_counts_each_item_it_builds_toward_the_size_limit`,
  `test_a_dicts_keys_count_toward_the_size_limit`,
  `test_a_dict_whose_keys_are_not_strings_counts_each_key_as_one_item`,
  `test_a_missing_field_of_a_dict_with_mixed_keys_is_a_language_error_not_a_crash`,
  `test_a_dict_is_read_at_any_key_it_holds`,
  `test_a_key_a_dict_cannot_hold_is_a_language_error_not_a_crash`,
  `test_an_arithmetic_result_too_large_for_a_number_is_a_language_error_not_a_crash`,
  `test_an_unnamed_command_skips_an_auto_name_the_record_already_holds`,
  `test_if_elif_else_runs_only_the_taken_branch_and_a_return_ends_the_program`,
  `test_a_command_inside_a_comprehension_reads_the_comprehensions_name`,
  `test_each_catalogue_repair_is_named_and_counted`,
  `test_a_cancelled_run_cancels_the_commands_its_program_launched_and_returns`. A pointer reaches an
  earlier line's name or the record: no cycle.

### D2: The repair catalogue is data, and each repair is counted ^d2

Serves C2. A table of repairs with a name, a matcher and a rewrite; the four readings of
[[ADR-0004-the-language#^c3|ADR-0004/C3]] are its first entries. The notification names the repair
as "<line>: read as X; if that is not what you meant, write it again", and the run counts repairs by
name so the bench can say which slips models make.

- **Landing evidence**: `test_the_tag_forms_repairs_are_counted_by_name`,
  `test_the_tag_form_counts_every_repair_one_call_needed`,
  `test_a_repair_is_counted_when_the_check_then_refuses_the_program`,
  `test_an_unknown_name_is_measured_against_the_nearest_known_name_in_both_forms`,
  `test_every_unknown_command_in_a_message_is_named_and_measured_not_only_the_first`, and in
  `tests/test_swe_solve.py`
  `test_the_row_counts_the_repairs_by_name_and_measures_each_unknown_name`: the bench row carries
  `repairs` by name and `unknown_names` as name, nearest known name and Levenshtein distance. Owed:
  a bench run taking the measurement, and the name repairs it turns on.

### D3: Note assignment and view curation in the dispatcher's settle ^d3

Serves C4 and C5. At settle the node's target is read: a note path writes the store. A RESULT counts
as consumed once a reader settled ok or its note was written. The runtime's hide, dated at that
settle or write, rides the next notification, or the closure when no turn follows
([[ADR-0006-the-notification#^c1|ADR-0006/C1]]); directives fold by date, so a later `context.show`
stands over it ([[ADR-0007-the-record#^c3|ADR-0007/C3]]). A reader in an untaken branch, or failed,
consumes nothing. A RESULT `OUT{}` names is never consumed; a consumed RESULT renders as a
placeholder.

- **Landing evidence**:
  `test_a_consumed_result_never_reaches_the_backend_yet_its_pointer_and_context_show_do`,
  `test_a_result_the_output_names_is_never_consumed`,
  `test_the_consumed_hide_survives_the_log_as_a_directive`,
  `test_the_context_figure_counts_a_consumed_result_as_its_placeholder`,
  `test_a_result_read_through_a_value_is_consumed_only_when_the_values_reader_settles_ok`,
  `test_a_failed_producer_writes_nothing_and_the_assignment_is_told_not_written`,
  `test_a_show_later_in_the_same_program_stands_over_the_hide_of_what_it_consumed`,
  `test_a_show_dated_with_the_branch_that_consumed_its_target_stands_over_the_hide` (a consumption
  precedes the entry it is dated at). The hide rides the notification's `meta["consumed"]`, beside
  the fold's `meta["directive"]`. Owed: the closure's half, with the closure (ADR-0006).

### D4: A value is bound for the run, and a computed one rides the record ^d4

Serves C3 and C6. A name is bound once; a value nothing has read stays bound past its turn. Its
first reader computes it, after the note writes before that reader; the walk reads a branch or an
output as it reaches it, a command's arguments when it starts; a write naming it comes after it,
reached or not. The notification carries an unread value as its expression (`meta["bound"]`), a
computed one as its value (`meta["values"]`); a replay binds both, running nothing. A failed
computation binds its failure.

- **Landing evidence**:
  `test_a_value_nothing_read_stays_bound_into_later_turns_and_its_name_stays_taken`,
  `test_a_value_computed_on_the_record_resolves_after_the_log_replays_it_and_nothing_runs_again`,
  `test_a_value_whose_computation_failed_stays_failed_and_what_it_ran_never_runs_again`,
  `test_a_value_bound_by_an_earlier_turn_reads_after_the_reading_programs_own_note_writes`,
  `test_a_value_bound_this_turn_reads_after_the_note_writes_before_its_first_reader_too`,
  `test_a_write_that_reads_a_value_comes_after_the_value_so_a_later_reader_never_waits_on_itself`,
  `test_a_value_nothing_read_is_bound_again_after_the_log_replays_it_and_reads_as_it_would_have`.
  Owed: a last turn's values on the closure (ADR-0006).

## Alternatives

| approach                                          | rejected because                                                                                          |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Interpret the message line by line                   | an interpreter that parses whole first is the same decision; the graph is chosen for scheduling and node identities, not for the preflight |
| Run the readable commands of a broken message        | the same partial state, plus a model that learns half its message ran; a syntax error refuses whole (C1)     |
| A general expression language (Python subset)     | `eval` is a second runtime with its own attack surface; the closed set covers select, join and filter     |
| A model call to filter or summarise results       | a second model on the data path costs a call and reads the whole result; the filter runs in the runtime   |
| `DEF` and `while` now                             | `DEF` is a token the symbolic parser does not yet read; named sub-programs are the next record; no unbounded loop |
| "Only what `OUT{}` names runs" (the scratch rule)  | told three times its edit had not run, a model rewrote the same message; every top-level command runs (C6)  |
| Rebinding a name across branches | not specified here: a name is bound once, and the inline `if` selects between aliases |
| A terminal `OUT{}` as the only exit | a model with its answer inside a branch must copy it out through the inline `if`; the control result costs one edge per block (C7) |
| Making the program form the only form | today's models write the three constructs well and the program form poorly; the profile chooses (C6)      |

## Consequences

- **S1**: A message is a small program; the model call count for select-join-filter work drops to
  one and the intermediate data never enters the context.
- **S2**: A refused message costs a turn as today; the fuzzy pass is expected to make refusals
  rarer, and its counts say by how much.
- **S3**: The symbolic grammar gains what it lacks today: an evaluator, an executor (the dispatcher)
  and a consumer. `DEF` and the remaining semantic operators land in the next record, same profile
  option.
- **S4**: A resumed chat replays programs by folding, as it replays directives; the long-running
  agent's continuous run has a checkpoint ([[ADR-0013-the-agent#^c7|ADR-0013/C7]]), no other run
  does.
- **S5**: The raw message is what the model wrote; the normalised program and the repairs applied
  ride the TEXT entry's `meta`, so the admitted reading can be audited after the catalogue changes.
  A version stamp names a parser and keeps nothing.
- **S6**: A list or dict stops at the part that takes it past the size limit: no later part runs,
  and the part that crossed it has run. A key a dict writes twice counts by its value at the time,
  so a dict that passes the limit on the way is refused though the key's last value would fit. The
  fields of `OUT{}` are one dict: past the limit together, the turn is a retry that names it.
  `test_a_list_or_dict_past_the_size_limit_runs_no_part_after_the_one_that_took_it_there`,
  `test_a_key_a_dict_writes_twice_counts_once_toward_the_size_limit` and
  `test_the_fields_of_out_are_held_together_to_the_size_limit_as_a_dict_literal_is` pin it.
- **S7**: Comprehensions let other tasks run every 100,000 items they examine, kept or rejected,
  counted once per task across nesting. So the deadline cancels one wherever it is computed: in a
  command's arguments, in a branch's test or an `OUT{}` field, where nothing after it runs, and in a
  value still computing once its turn's commands settle, whether the settle returns or raises.
  `test_a_long_comprehension_lets_other_tasks_run_every_limit_items_it_examines_rejected_ones_too`,
  `test_the_runs_deadline_cancels_a_command_whose_argument_is_still_a_long_comprehension`,
  `test_the_runs_deadline_stops_a_branch_test_or_an_output_field_still_in_a_long_comprehension`,
  `test_a_value_whose_reader_the_deadline_cancelled_stops_computing_before_the_run_returns`,
  `test_a_walk_whose_settle_raises_still_stops_the_value_it_was_computing` and
  `test_a_walk_whose_settle_raises_stops_a_value_waiting_on_something_no_budget_ends` pin it.
- **S8**: The run's time budget is the only bound on a comprehension's total work: without one it is
  unbounded by design, and no number of rejected items stops it, while the items it keeps stop it at
  the size limit. The deadline is cooperative: a comprehension is cut at a 100,000-item yield, and
  the loop runs one more interval first, so for scalar items the overrun is under two intervals.
  Between yields each kept item is sized synchronously, so one large item holds the loop for its own
  scan.
- **S9**: A `run.more` that raises the time budget moves the deadline of a walk computing its own
  expressions and of the settlement of the turn's commands, argument evaluation included, so what
  follows runs to the new deadline. A refused grant moves nothing and the late command is cancelled.
  A grant after the walk's own deadline fired raises the bound for what follows the walk; nothing
  the walk held commits. The reap at the run's end has no deadline.
  `test_a_time_extension_granted_inside_the_walk_moves_the_deadline_the_walk_runs_under`,
  `test_a_grant_of_time_by_a_commands_argument_moves_the_deadline_its_turns_settle_waits_under`,
  `test_a_grant_of_time_while_the_turns_commands_settle_moves_the_deadline_they_settle_under`,
  `test_a_refused_grant_during_settlement_leaves_the_deadline_and_the_late_command_cancelled`,
  `test_a_grant_of_time_moves_the_deadline_of_every_limit_a_run_has_registered`,
  `test_a_settle_cancelled_while_it_waits_leaves_no_time_limit_on_the_run`,
  `test_a_grant_of_time_during_the_reap_gives_it_no_deadline_so_a_cleanup_runs_to_its_end` and
  `test_a_grant_of_time_landing_after_a_walks_deadline_fired_raises_the_bound_and_moves_nothing` pin
  it.
- **S10**: A `TimeoutError` raised by what the walk reads is that error: only the walk's own
  deadline reads as the budget spent. That refusal names the budget the walk ran under, and a grant
  that landed after the deadline fired beside it. It stays on the run's `refused` list; the spent
  budget ends the run before a notification tells it, and the closure names the outcome and the
  cancelled commands, no refusal.
  `test_a_timeout_error_from_a_store_read_is_that_error_not_the_time_budget_spent`,
  `test_the_runs_deadline_stops_a_branch_test_or_an_output_field_still_in_a_long_comprehension`,
  `test_a_walk_a_grant_missed_names_the_budget_it_ran_under_and_the_late_grant`,
  `test_the_same_walk_without_the_grant_is_cut_at_the_deadline_it_began_with` and
  `test_a_walk_a_grant_reached_in_time_names_the_raised_budget_when_it_runs_out` pin it.
- **S11**: The deadline lands at the walk's next yield. A write reached after the walk's own
  deadline fired, or the budget is spent, is refused as spent before anything of it runs: no command
  is launched, no argument evaluated and no hook called, whatever a later grant did or its
  evaluation would raise. It commits nothing and consumes no result it read; the turn is refused as
  the budget spent, once however many writes were cut.
  `test_a_write_reached_past_the_deadline_with_nothing_yielding_commits_nothing`,
  `test_a_write_reached_past_the_deadline_launches_no_command_and_runs_no_hook`,
  `test_a_write_reached_past_the_deadline_is_refused_as_spent_whatever_its_evaluation_would_raise`,
  `test_a_write_queued_with_time_left_is_refused_as_spent_whatever_its_evaluation_raises`,
  `test_a_write_waiting_when_the_walk_expired_commits_nothing_whatever_a_late_grant_did`,
  `test_a_write_refused_for_the_spent_budget_consumes_none_of_the_results_it_read` and
  `test_a_write_queued_before_the_walk_was_cut_commits_nothing_and_the_turn_is_refused_once` pin it.
- **S12**: A write the settlement's own deadline cuts while it waits for its command is the budget
  spent too: the turn says so once, naming the budget the settlement ran under. A cut command no
  write waits for is a cancelled result and no refusal.
  `test_a_write_the_settlement_cut_while_it_waited_for_its_command_is_refused_as_spent_once`,
  `test_a_write_the_settlement_cut_after_a_grant_names_the_budget_the_settlement_ran_under` and
  `test_a_command_the_settlement_cut_is_a_cancelled_result_and_not_a_refusal_of_the_turn` pin it.
