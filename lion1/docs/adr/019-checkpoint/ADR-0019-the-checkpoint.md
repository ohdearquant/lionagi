---
adr: ADR-0019
status: draft
liveness: operating (the take, the transcript scan, the restore, the files and both transports are pinned by tests; the take's own checks of kind, reason and injected text have no test; lanes from pull requests and threads are owed; nothing in the code starts a Claude Code session's checkpoint)
date: "2026-09-25"
area: checkpoint
kind: new
depends_on:
  - "[[ADR-0006-the-notification|ADR-0006]]"
  - "[[ADR-0007-the-record|ADR-0007]]"
  - "[[ADR-0009-backends|ADR-0009]]"
  - "[[ADR-0013-the-agent|ADR-0013]]"
  - "[[ADR-0016-the-lion-command|ADR-0016]]"
  - "[[ADR-0017-the-desk|ADR-0017]]"
tags:
  - adr
  - runtime
  - checkpoint
  - context
---

# ADR-0019: The checkpoint and the restore

## Context

A Claude Code session's state is its transcript and the files around it: the Monitors it armed, the
Agents it spawned, the words the person typed, the figure its statusline shows. Continuing the
session reads the whole transcript again; a fresh one starts with none of it. A resident's next
process loses the view its record folded to. This record fixes what a checkpoint of either holds,
how it is taken with no model call, where it lands, and what a restore gives.

When a resident takes a checkpoint and how its next process replays it is
[[ADR-0013-the-agent#^c7|ADR-0013/C7]]; the fold a resident's view follows is
[[ADR-0007-the-record|ADR-0007]]; the context figure is [[ADR-0006-the-notification|ADR-0006]];
`lion context` and the areas server that carry the verbs are [[ADR-0016-the-lion-command|ADR-0016]]
and [[ADR-0017-the-desk|ADR-0017]]. Source: `lionagi/checkpoint.py`, `hub/context.py`,
`apps/cli/lion_cli/context.py`, `context_api` in `apps/cli/lion_cli/areas.py`, `_checkpoint` and
`_restore` in `hub/agent/agent.py`.

## Definitions

- **checkpoint**: the named values and the folded view of a Claude Code session or a resident's run,
  taken with no model call and written under `.khive/context/`, from which a restore starts the next
  session or process.
- **take**: one checkpoint made: the named values read or declared, the view folded, one file
  written.
- **named value**: one `Var` of a checkpoint: a name, a kind, the value, where it was read from and
  when.
- **exchange**: one prompt of a Claude Code session, typed by the person or opened by a task
  notification, with the last text the session wrote before the next prompt.
- **injected text**: what a hook or the Claude Code harness writes into a user row: a slash command
  with its caveat and output, a task notification, a system reminder, an interrupt mark.
- **lazy value**: a named value over 400 characters, other than a `directive` or a `watch`, which a
  restore shows by name, size and how to load it.
- **restore**: what a checkpoint gives the next session or process: the first prompt of a fresh
  Claude Code session, or a resident's record as its first run's `history`.

## Assumptions

| # | statement | source | if false |
| - | --------- | ------ | -------- |
| A1 | The transcript stays on disk after the session ends; a checkpoint points into it and never copies a tool call or its result. | `source.transcript`; a folded exchange is named by its row's uuid and time, and the restore prints the path | the folded exchanges are lost with the file, and the checkpoint must carry them |
| A2 | Every value a fresh session needs is declared, read from a file the session keeps, or read from a store. | each kind is read by a function in `hub/context.py` or declared through `lion context set` | the checkpoint needs a model call to write the rest, and is prose again |
| A3 | A Monitor ends at its deadline, 30 minutes at most, whatever `persistent` says. | `MONITOR_CAP_MS` in `hub/context.py`; 65 Monitor calls read from 18 days of one session directory's transcript, all persistent, none still running (2026-09-24) | a Monitor still running is left out of the restore and never re-armed |
| A4 | A spawned Agent with no ending task notification 12 hours after its start died with a session restart. | `LEG_HOURS` in `hub/context.py`; without the bound a take read 29 spawns as running, the oldest two days old (2026-09-24) | a spawn running past 12 hours is left out of the restore |

## Claims

### C1: A take reads named values and a folded view, and calls no model _(enforced: mechanical)_ ^c1

- **Subject**: `take` in `lionagi/checkpoint.py` and its callers: `lion context checkpoint`, the
  `/api/context/checkpoint` route, the resident's `_checkpoint`.
- **Violated when**: a take reaches a backend, a value holds text a model wrote for the checkpoint,
  a kind or reason outside the lists is kept, or a tool call or its result is held inline.

A checkpoint is an id, the directory's actor, the time, the reason, the source, the named values and
the view. The kinds are `fact`, `watch`, `lane`, `directive`, `topic`, `posture` and one for spawned
Agents running at the take. The reasons are `threshold`, `rotate`, `precompact`, `manual` and the
one a resident writes when its run ends.

A Claude Code session's view is its exchanges (C3). A resident's is its record after `fold`: entries
by name, hidden names and summaries, the record staying where `source.record` points. A later value
of a name replaces an earlier, so a declared value stands over one read from the session.

### C2: An exchange is the person's prompt and the session's last text, with nothing injected _(enforced: mechanical)_ ^c2

- **Subject**: the transcript scan in `hub/context.py` (`_read`, `_prompt`, `_clean`), and `take`
  over a Claude Code view.
- **Violated when**: a tool call or result, thinking, a compaction summary, a meta or system-sourced
  row, or injected text is read as a prompt; an earlier text stands as the exchange's; or a take
  keeps an input or `directive` holding injected text.

A prompt is a `user` row whose content is a string, or text blocks beside an image, that no hook or
harness wrote. A row opening with a slash command's name is not a prompt; a system reminder inside a
typed prompt is cut out. The session's last text block before the next prompt is the exchange's
text.

A task notification opens an exchange of its own, labelled `[event]` in 200 characters at most,
never a `directive`. The same words again with no answer between are one exchange. The take's own
refusal of injected text has no test.

### C3: The kept view is half the budget, oldest folded first, and the person's words are eight at most _(enforced: mechanical)_ ^c3

- **Subject**: `fold_turns` in `lionagi/checkpoint.py`, `directives` in `hub/context.py`, and
  `render_prompt`.
- **Violated when**: the kept exchanges estimate over half the budget while more than one is kept,
  the latest exchange is folded, a folded one goes unnamed, or a restore carries more than eight
  `directive` values.

Each exchange is sized at four characters per token. The oldest fold first until the rest fits half
the budget, the size the fold event aims at ([[ADR-0007-the-record#^c2|ADR-0007/C2]]); the latest
always stays. A folded exchange is named by its row's uuid and time, and the transcript keeps it.
`lion context checkpoint` takes a budget of 8,000 tokens unless given one.

The `directive` values are the ring a prompt hook keeps for the session when it holds any, else the
newest prompts the person typed; a prompt another program typed, as its typing log records, is left
out. Either way they are eight at most, and a restore prints the newest eight it is handed.

### C4: The restore's first line re-arms every `watch` verbatim before any other work _(enforced: mechanical)_ ^c4

- **Subject**: `render_prompt` and `lion context restore`.
- **Violated when**: a `watch` the checkpoint holds is missing from the prompt or altered, the
  prompt asks for anything before the re-arm, or a lazy value's body is printed.

The first line names the checkpoint, the actor, the time, the reason and the context figure at the
take, and asks for each `watch` to be re-armed with the Monitor tool before any other work. Each
command follows in a fence longer than any backtick run inside it, so it reads back byte for byte.

Then come a declared `mcp` fact as the MCP servers expected, the person's words verbatim and oldest
first, the lanes, the running spawns, the topic and the other facts. The kept exchanges and the
`posture` values follow, and last what to do first: a declared `next` fact, else re-arm and resume.
A lazy value shows its name, size and loader: its `ref` or `lion context show`.

### C5: A `watch` is a Monitor still running, and a spawn an Agent still running _(enforced: mechanical)_ ^c5

- **Subject**: `watches` and the spawn scan in `hub/context.py`.
- **Violated when**: a Monitor that failed to arm, is past its deadline, or is named by a later
  TaskStop or ending task notification is kept; one command yields two `watch` values; or a
  completed, stopped or older spawn counts as running.

A Monitor's deadline is its timeout, five minutes when none is given, capped at 30 minutes whatever
`persistent` says (A3). Of one command armed twice the newest arm decides, running or not. An inbox
Monitor for the directory's actor is added when no kept `watch` runs the inbox script. A spawn
counts only when its result named an agent id and it started within 12 hours (A4). A timestamp with
no zone is read as UTC.

### C6: A checkpoint lands only where git ignores it, and no take overwrites another _(enforced: mechanical)_ ^c6

- **Subject**: `save` and `assert_ignored` in `lionagi/checkpoint.py`, `set_var` and `checkpoint` in
  `apps/cli/lion_cli/context.py`, the resident's `_checkpoint`.
- **Violated when**: a file lands under a `.khive/context/` a git work tree does not ignore, a git
  that cannot answer reads as ignored, a take replaces another, `latest.json` names no file, or a
  declared value is lost.

A checkpoint is `<dir>/.khive/context/<id>.json`, the id `ckpt-` with the date, the time and the
actor. It is created only when absent and readable by its owner alone; an id already there gains
`-02`, `-03`. `latest.json` is then replaced whole, naming it. Inside a work tree `git check-ignore`
must say ignored; outside a repository the take proceeds; a refusal writes nothing.

Declared values wait in `pending.jsonl` under a lock. A checkpoint drains the text it read, and a
value declared since stays; no test pins that second half.

### C7: The figure at the take is the session's own, or not known _(enforced: mechanical)_ ^c7

- **Subject**: `context_figure` in `hub/context.py`, `lion context status`, and the restore's first
  line.
- **Violated when**: another session's figure is taken, an absent or unreadable figure fails the
  take or reads as a number, or another absent source fails it.

A Claude Code session's figure is the statusline's per-session file named by the session id: the
input tokens, the window and the percentage. Absent, unreadable or not an object, each field is null
and the restore says "not known". A resident's is the measure its fold decides on, the view estimate
plus the excess above the floor ([[ADR-0009-backends#^c3|ADR-0009/C3]]), over `context_budget`.

An absent topic, loop file or hook ring gives no value; a khive that cannot answer leaves its reason
in `source.lanes`. Of the places read, a missing transcript, a `.khive/config.toml` naming no actor
and a `pending.jsonl` line that is not a declared value refuse the take.

## Decisions

### D1: `lionagi/checkpoint.py`: `Var`, `Checkpoint`, `take`, `fold_turns`, `render_prompt`, `save`, `load`, `to_history` ^d1

Serves C1, C3, C4 and C6. `take` is pure: no model call, no file. `save` writes and `load` reads by
id, the latest when none is named, refusing an id that is not a plain name. `to_history` is a
resident's restore: the record it points at, read back to the seq taken, each row through
`entry_of`, refused unless it folds again to the view taken, and refused for a Claude Code
checkpoint, whose restore is the prompt.

- **Landing evidence**: `tests/test_checkpoint.py`:
  `test_the_first_line_re_arms_each_watch_verbatim_before_any_work`,
  `test_concurrent_saves_each_land_and_latest_names_one_of_them`,
  `test_a_take_refused_by_the_gitignore_writes_nothing_and_an_ignored_one_lands`, the long-value
  test; `tests/test_chat.py`:
  `test_a_checkpoint_of_the_record_resumes_the_view_the_run_last_showed`.

### D2: `hub/context.py`: one function per place a Claude Code session keeps state ^d2

Serves C2, C3, C5 and C7. The transcript is the newest under Claude Code's project directory for the
session's directory, never a pre-compaction copy, scanned once per size and time for every function.
Beside it: the hook ring, the typing log, `active_topic` in the project's sessions registry, the
actor's active tasks in khive, and the statusline's per-session file. The newest loop file under
`.khive/loop/` gives its header as a `fact` and its `## Posture` as a `posture`; the last text, its
LNDL taken out, is a `posture` too.

- **Landing evidence**: `tests/test_checkpoint.py`:
  `test_the_reader_keeps_each_prompt_with_its_final_text_and_no_dropped_kind_survives`,
  `test_each_source_harvests_its_kind_and_an_absent_source_is_a_value`,
  `test_watches_are_the_monitors_still_running_verbatim_and_the_inbox_line_is_added_once`,
  `test_a_prompt_the_harness_requeued_is_one_turn`,
  `test_the_ceiling_eight_directives_half_the_budget_and_the_watch_list`, the spawn test.

### D3: `lion context` and `/api/context/*`: one implementation, two transports ^d3

Serves C1, C4, C6 and C7. `apps/cli/lion_cli/context.py` holds `set`, `checkpoint`, `restore`,
`status` and `show`, run from the session's directory; a refusal prints its reason on standard error
and exits 1. The areas server's `context_api` routes all but `show` to the same functions for a
directory its registry names, 404 for any other and 409 on a refusal, behind the server's Host and
Origin gates. `status` says stale when no checkpoint exists or the file it points at changed after
it.

- **Landing evidence**: `tests/test_checkpoint.py`:
  `test_lion_context_set_checkpoint_show_restore_and_status_through_the_cli`,
  `test_the_context_routes_answer_for_a_registered_desk_only`,
  `test_the_served_context_routes_sit_behind_the_same_gates`,
  `test_no_model_is_called_by_the_checkpoint_the_restore_or_the_status`.

### D4: The resident's arm in `hub/agent/agent.py` ^d4

Serves C1, C6 and C7. When the resident takes and replays is [[ADR-0013-the-agent#^c7|ADR-0013/C7]].
Its take holds the `posture` by its note key, the last wake and the unreported batch by their keys
in the agent's note file, and each value the run declared. Its record is written beside the
checkpoint as `record-<run>.jsonl`, rewritten whole at each take, after the same ignore check. A
take that fails is kept among the agent's errors and the run goes on.

- **Landing evidence**: `tests/test_checkpoint.py`:
  `test_the_resident_checkpoints_at_its_fold_and_its_next_process_resumes_the_same_view`,
  `test_the_resident_checkpoints_when_stop_ends_its_run`,
  `test_a_restore_that_cannot_replay_is_said_and_the_run_starts_fresh`.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Continuing the old session with `claude -c` | the whole transcript is read again, and nothing chose what to keep |
| A model's summary as the checkpoint | a model's opinion of the session, unverifiable and unfoldable; a summary is at most one declared value |
| Copying tool calls and their results into the checkpoint | it grows with the session and the fold gains nothing; the transcript keeps them (A1) |
| Re-arming every Monitor that says `persistent` | the tool ends every Monitor at its deadline, and none of 65 persistent ones read was still running (A3) |
| The statusline's last-rendered file as the figure | it belongs to whichever session rendered last; the per-session file is named by the session id (C7) |
| One file per directory, overwritten at each take | a take would erase the one before; each take is its own file and `latest.json` names the newest (C6) |

## Consequences

- **S1**: Nothing in the code takes a Claude Code session's checkpoint by itself or starts the fresh
  session: a person, a hook or another program runs `lion context checkpoint`, starts a session and
  gives it the restore's prompt. The reasons `threshold`, `rotate` and `precompact` name such
  callers.
- **S2**: The statusline's per-session file and the hook ring are written outside the code, by the
  statusline command and the prompt hook a Claude Code install configures; without them the figure
  reads "not known" and the person's words come from the transcript.
- **S3**: `save` and `load` check no kind and no reason: a file edited to hold another kind loads,
  and the restore leaves out a value of a kind outside the list. No test pins it.
- **S4**: Reading open pull requests and threads awaiting a reply as lanes is owed. Today the lanes
  are the actor's active khive tasks, and `source.lanes` says the rest were not read.
- **S5**: One `latest.json` serves both arms. A Claude Code checkpoint taken in a resident's
  directory, the only kind of directory the `/api/context/` routes reach, becomes the latest; the
  resident's next process refuses it and starts fresh ([[ADR-0013-the-agent|ADR-0013]] S9). No test
  pins it.
- **S6**: `hub/context.py` reads fixed places under the home directory: Claude Code's project
  directory, the hook ring, the typing log and the statusline's files; where no tool writes one, it
  reads as absent. The script the added inbox Monitor runs is one absolute path, so on another host
  that Monitor names a script that is not there.
- **S7**: The code removes no checkpoint: every take adds a file under `.khive/context/`, and each
  resident run's record file stays beside them.
- **S8**: The boundary: the durable record and a session's continuity across processes are the
  kernel's: which checkpoint a new process starts from, who starts it, how long a checkpoint is
  kept, and where it lives once the directory is gone. Here a checkpoint is files in a directory git
  ignores, and a restore is a prompt or a history.
