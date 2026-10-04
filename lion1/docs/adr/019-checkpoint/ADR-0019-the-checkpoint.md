---
adr: ADR-0019
status: draft
liveness: operating (the take and its refusals, the transcript scan, the lanes from tasks, threads and pull requests with the shape of each row they read, the restore, the files, both transports, the status's staleness to the take's fraction of a second the PreCompact hook that starts a Claude Code session's checkpoint, and a Codex session's take from its rollout and its restore are pinned by tests)
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
Agents it spawned, the words the person typed, the figure its statusline shows. A Codex session's is
its rollout: the person's prompts, the sub-agents it spawned, the figure of its last request.

Continuing the session reads the whole transcript again; a fresh one starts with none of it. A
resident's next process loses the view its record folded to. This record fixes what a checkpoint of
either holds, how it is taken with no model call, where it lands, and what a restore gives.

When a resident takes a checkpoint and how its next process replays it is
[[ADR-0013-the-agent#^c7|ADR-0013/C7]]; the fold a resident's view follows is
[[ADR-0007-the-record|ADR-0007]]; the context figure is [[ADR-0006-the-notification|ADR-0006]];
`lion context` and the areas server that carry the verbs are [[ADR-0016-the-lion-command|ADR-0016]]
and [[ADR-0017-the-desk|ADR-0017]]. Source: `src/lion1/checkpoint.py`, `hub/backend/checkpoint.py`,
`hub/backend/claude_code/checkpoint.py`, `hub/backend/codex_checkpoint.py`,
`apps/cli/lion_cli/context.py`, `context_api` in `apps/cli/lion_cli/areas.py`, `_checkpoint` and
`_restore` in `hub/agent/agent.py`.

## Definitions

- **checkpoint**: the named values and the folded view of a Claude Code or Codex session or a
  resident's run, taken with no model call and written under `.khive/context/`, from which a restore
  starts the next session or process.
- **take**: one checkpoint made: the named values read or declared, the view folded, one file
  written.
- **named value**: one `Var` of a checkpoint: a name, a kind, the value, where it was read from and
  when.
- **exchange**: one prompt of a Claude Code or Codex session, typed by the person or, in Claude
  Code, opened by a task notification, with the last text the session wrote before the next prompt.
- **injected text**: what a hook or the Claude Code harness writes into a user row: a slash command
  with its caveat and output, a task notification, a system reminder, an interrupt mark. For Codex,
  what its CLI writes into a user message: the environment, AGENTS.md, the plugin list.
- **lazy value**: a named value over 400 characters, other than one of a kind its taker names eager
  (a Claude Code session's `directive` and `watch`, a Codex session's `directive`), which a restore
  shows by name, size and how to load it.
- **restore**: what a checkpoint gives the next session or process: the first prompt of a fresh
  Claude Code or Codex session, or a resident's record as its first run's `history`.
- **rollout**: the file a Codex session writes under `$CODEX_HOME/sessions`, one JSON row per line,
  the first its own `session_meta`.

## Assumptions

| # | statement | source | if false |
| - | --------- | ------ | -------- |
| A1 | The transcript stays on disk after the session ends; a checkpoint points into it and never copies a tool call or its result. | `source.transcript`; a folded exchange is named by its row's uuid and time, and the restore prints the path | the folded exchanges are lost with the file, and the checkpoint must carry them |
| A2 | Every value a fresh session needs is declared, read from a file the session keeps, or read from a store. | each kind is read by a function in `hub/backend/checkpoint.py`, `hub/backend/claude_code/checkpoint.py` or `hub/backend/codex_checkpoint.py`, or declared through `lion context set` | the checkpoint needs a model call to write the rest, and is prose again |
| A3 | A Monitor ends at its deadline, 30 minutes at most, whatever `persistent` says. | `MONITOR_CAP_MS` in `hub/backend/claude_code/checkpoint.py`; 65 Monitor calls read from 18 days of one session directory's transcript, all persistent, none still running (2026-09-24) | a Monitor still running is left out of the restore and never re-armed |
| A4 | A spawned Agent with no ending task notification 12 hours after its start died with a session restart. | `SPAWN_HOURS` in `hub/backend/checkpoint.py`; without the bound a take read 29 spawns as running, the oldest two days old (2026-09-24) | a spawn running past 12 hours is left out of the restore |
| A5 | A Codex rollout marks each item of a user message with what wrote it, and every item the CLI writes there carries a kind other than `user.text`. | `content_item_kinds`, read by `_typed` in `hub/backend/codex_checkpoint.py`; all 7,388 rollouts of September and October 2026, CLI 0.153.4 to 0.160.0: 14,470 user messages marked each item, no `user.text` item opened as the CLI's text does, and every other text item did (2026-10-04) | text the CLI wrote is read as the person's, or the take refuses it by its opening; a message marking no item is counted in `source.unmarked_messages`, never read |
| A6 | A Codex sub-agent whose last activity row is its start or an `interacted` row still holds work. | `_RUNNING` in `hub/backend/codex_checkpoint.py`; in October's rollouts the row after an `interacted` one was `completed` 303 times, `interacted` 231, `interrupted` 6, and 7 of 834 sub-agents a thread started ended on neither; the 12 hours of A4 are not measured on Codex (2026-10-04) | a sub-agent that finished is restored as running, until 12 hours after its start |

## Claims

### C1: A take reads named values and a folded view, and calls no model _(enforced: mechanical)_ ^c1

- **Subject**: `take` in `src/lion1/checkpoint.py`, `take_session` in
  `hub/backend/claude_code/checkpoint.py`, and their callers: `lion context checkpoint`, the
  `/api/context/checkpoint` route, the resident's `_checkpoint`.
- **Violated when**: a take reaches a backend, a value holds text a model wrote for the checkpoint,
  a kind or reason outside the lists is kept, or a tool call or its result is held inline.

A checkpoint is an id, the directory's actor, the time, the reason, the source, the named values and
the view. Kinds and reasons are the taker's lists: a run's are `fact`, `posture` and `threshold`,
`manual`, `end`; a Claude Code session adds `watch`, `lane`, `directive`, `topic`, `spawn` and
`rotate`, `precompact`.

A Claude Code session's view is its exchanges (C3). A resident's is its record after `fold`: entries
by name, hidden names and summaries, the record staying where `source.record` points. A later value
of a name replaces an earlier, so a declared value stands over one read from the session.

### C2: An exchange is the person's prompt and the session's last text, with nothing injected _(enforced: mechanical)_ ^c2

- **Subject**: the transcript scan in `hub/backend/claude_code/checkpoint.py` (`_read`, `_prompt`,
  `_clean`), and `take_session`.
- **Violated when**: a tool call or result, thinking, a compaction summary, a meta or system-sourced
  row, or injected text is read as a prompt; an earlier text stands as the exchange's; or a take
  keeps an input or `directive` holding injected text.

A prompt is a `user` row whose content is a string, or text blocks beside an image, that no hook or
harness wrote. A row opening with a slash command's name is not a prompt; a system reminder inside a
typed prompt is cut out. The session's last text block before the next prompt is the exchange's
text.

A task notification opens an exchange of its own, labelled `[event]` in 200 characters at most,
never a `directive`. The same words again with no answer between are one exchange. The take's
refusal of injected text is pinned by
`test_a_session_take_refuses_a_hooks_frame_in_a_directive_or_a_kept_input`.

### C3: The kept view is half the budget, oldest folded first, and the person's words are eight at most _(enforced: mechanical)_ ^c3

- **Subject**: `fold_turns` (`hub/backend/checkpoint.py`), `directives` and `render_prompt` in
  `hub/backend/claude_code/checkpoint.py`.
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

- **Subject**: `render_prompt` in `hub/backend/claude_code/checkpoint.py` and `lion context
  restore`.
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

- **Subject**: `watches` and the spawn scan in `hub/backend/claude_code/checkpoint.py`.
- **Violated when**: a Monitor that failed to arm, is past its deadline, or is named by a later
  TaskStop or ending task notification is kept; one command yields two `watch` values; or a
  completed, stopped or older spawn counts as running.

A Monitor's deadline is its timeout, five minutes when none is given, capped at 30 minutes whatever
`persistent` says (A3). Of one command armed twice the newest successful arm decides, running or
not; an arm that failed is never kept and retires no older arm. An inbox Monitor for the directory's
actor is added when no kept `watch` runs the inbox script. A spawn counts only when its result named
an agent id and it started within 12 hours (A4). A timestamp with no zone is read as UTC.

### C6: A checkpoint lands only where git ignores it, and no take overwrites another _(enforced: mechanical)_ ^c6

- **Subject**: `save` in `src/lion1/checkpoint.py`, `context_dir` and `assert_ignored` in
  `hub/khive.py`, `set_var` and `checkpoint` in `apps/cli/lion_cli/context.py`, the resident's
  `_checkpoint`.
- **Violated when**: a file lands under a `.khive/context/` a git work tree does not ignore, a git
  that cannot answer reads as ignored, a take replaces another, `latest.json` names no file, or a
  declared value is lost.

A checkpoint is `<dir>/.khive/context/<id>.json`, the id `ckpt-` with the date, the time and the
actor. It is created only when absent and readable by its owner alone; an id already there gains
`-02`, `-03`. `latest.json` is then replaced whole, naming it. Inside a work tree `git check-ignore`
must say ignored; outside a repository the take proceeds; a refusal writes nothing. Core's `save`
writes where it is told; each caller runs the hub's check first.

Declared values wait in `pending.jsonl` under a lock. A checkpoint drains the text it read, and a
value declared since stays; no test pins that second half.

### C7: The figure at the take is the session's own, or not known _(enforced: mechanical)_ ^c7

- **Subject**: `context_figure` in `hub/backend/claude_code/checkpoint.py`, `lion context status`,
  and the restore's first line.
- **Violated when**: another session's figure is taken, an absent or unreadable figure fails the
  take or reads as a number, or another absent source fails it.

A Claude Code session's figure is the statusline's per-session file named by the session id: the
input tokens, the window and the percentage. Absent, unreadable or not an object, each field is null
and the restore says "not known". A resident's is the measure its fold decides on, the view estimate
plus the excess above the floor ([[ADR-0009-backends#^c3|ADR-0009/C3]]), over `context_budget`. A
Codex session's is its rollout's (C9).

An absent topic, loop file or hook ring gives no value; a khive or a gh that cannot answer leaves
its reason in `source.lanes`. Of the places read, a missing transcript, a `.khive/config.toml`
naming no actor and a `pending.jsonl` line that is not a declared value refuse the take.

### C8: A Codex session's exchange is the person's prompt, never text its CLI wrote _(enforced: mechanical)_ ^c8

- **Subject**: the rollout scan in `hub/backend/codex_checkpoint.py` (`_read`, `_typed`),
  `directives` and `take_codex`.
- **Violated when**: an item not marked `user.text`, or a forked thread's parent row, is read as a
  prompt; a sub-agent's prompt is a `directive`; an earlier text stands as the turn's; or a take
  keeps a `directive` or input opening with the CLI's text.

A prompt is the `input_text` items of a user message its row marks `user.text` (A5). A turn's text
is the last assistant text before the next prompt; reasoning, tool calls and compactions are never
read into one. The same words again with no answer between are one exchange.

The first `session_meta` is the thread's own. A forked thread's rows before its own history starts
are its parent's and are not read. The `directive` values are the newest eight prompts, none in a
thread a sub-agent runs. Pinned by
`test_a_rollout_reads_each_typed_prompt_with_its_turns_last_text_and_nothing_the_cli_wrote` and
`test_a_forked_sub_agents_rollout_reads_its_own_rows_and_its_words_are_not_the_persons`.

### C9: A Codex session's spawns and figure are its rollout's, and its restore re-arms nothing _(enforced: mechanical)_ ^c9

- **Subject**: `spawns`, `context_figure`, `take_codex` and `render_prompt` in
  `hub/backend/codex_checkpoint.py`, `lion context checkpoint --codex` and `status --codex`.
- **Violated when**: a completed or interrupted sub-agent, another thread's, or one started 12 hours
  before the take counts as running; another thread's figure is taken or an absent one reads as a
  number; or a take or restore holds a `watch`, `precompact` or re-arm.

A spawn is a sub-agent the thread started whose last activity row is its start or more work (A6),
within 12 hours. The figure is the last `token_count` holding one: the last request's input tokens
over the model's window, else not known.

The kinds are a run's with `spawn`, `lane`, `directive` and `topic`, the reasons a run's with
`rotate`: no Monitor is there to re-arm, and `precompact` names Claude Code's hook. The restore's
first line names the thread, model and CLI; the rest is C4's without the watches. Pinned by
`test_a_spawn_is_a_sub_agent_the_rollout_shows_holding_work_within_the_bound` and
`test_the_codex_restore_names_the_thread_first_and_asks_for_no_re_arm`.

## Decisions

### D1: `src/lion1/checkpoint.py`: `Var`, `Checkpoint`, `take`, `listed`, `save`, `load`, `to_history` ^d1

Serves C1 and C6. `take`, `save` and `load` check the caller's kinds and reasons, a run's by
default. `save` and `load` work in the directory handed them, by id, the latest by default, refusing
an id that is not a plain name. `to_history` is a run's restore: its record read back to the seq
taken, each row through `entry_of`, refused unless it folds to the view taken. One holding no run's
record is refused. The replay checks content: a row's text and a result's fields, its value aside.

- **Landing evidence**: `tests/test_checkpoint.py`:
  `test_concurrent_saves_each_land_and_latest_names_one_of_them`,
  `test_a_record_whose_rows_were_rewritten_is_refused_though_it_folds_to_the_same_view`,
  `test_save_refuses_an_id_that_is_not_a_name_and_writes_nothing`,
  `test_save_and_load_refuse_a_why_or_a_kind_off_the_lists_and_name_the_value`,
  `test_a_row_cut_inside_a_character_past_the_take_is_never_decoded_and_one_inside_it_is_named`, the
  long-value test; `tests/test_chat.py`:
  `test_a_checkpoint_of_the_record_resumes_the_view_the_run_last_showed`;
  `tests/test_khive_context.py`:
  `test_assert_ignored_reads_gits_two_answers_and_refuses_when_git_cannot_answer`,
  `test_the_guard_refuses_a_folder_git_would_commit_and_a_checkpoint_saved_past_it_stays_untracked`.

### D2: `hub/backend/claude_code/checkpoint.py`: the session's take and restore, one function per place it keeps state ^d2

Serves C2 to C5 and C7. `take_session` takes on the session's lists over `fold_turns` and refuses
injected text; `render_prompt` is the restore. The transcript is the newest under Claude Code's
project directory for its directory, never a pre-compaction copy, read once per size and time.
Beside it: the hook ring, the typing log, `active_topic` in the project's sessions registry, the
actor's lanes (S4), and the statusline's per-session file. The newest loop file's header is a `fact`
and its `## Posture` a `posture`, as is the last text less its LNDL.

- **Landing evidence**: `tests/test_context.py`:
  `test_the_reader_keeps_each_prompt_with_its_final_text_and_no_dropped_kind_survives`,
  `test_each_source_harvests_its_kind_and_an_absent_source_is_a_value`,
  `test_watches_are_the_monitors_still_running_verbatim_and_the_inbox_line_is_added_once`,
  `test_a_prompt_the_harness_requeued_is_one_turn`,
  `test_the_ceiling_eight_directives_half_the_budget_and_the_watch_list`,
  `test_legs_are_agents_the_transcript_shows_running`; `tests/test_claude_code_checkpoint.py`:
  `test_the_first_line_re_arms_each_watch_verbatim_before_any_work`,
  `test_the_restore_refuses_a_why_or_a_kind_off_the_lists_and_names_the_value`;
  `tests/test_context.py`:
  `test_the_lanes_are_the_active_tasks_the_threads_awaiting_a_reply_and_the_open_pull_requests`,
  `test_a_lane_source_that_cannot_answer_says_why_and_a_full_page_says_it_is_one`,
  `test_a_row_of_the_wrong_shape_is_counted_invalid_and_never_aborts_the_take`.

### D3: `lion context` and `/api/context/*`: one implementation, two transports ^d3

Serves C1, C4, C6 and C7. `apps/cli/lion_cli/context.py` holds `set`, `checkpoint`, `restore`,
`status` and `show`, run from the session's directory; a refusal prints its reason on standard error
and exits 1. `checkpoint --hook` is the PreCompact hook's entry (S1). The areas server's
`context_api` routes all but `show` to the same functions for a directory its registry names, 404
for any other and 409 on a refusal, behind the server's guards
([[ADR-0017-the-desk#^c11|ADR-0017/C11]]). `status` says stale when no checkpoint exists or the file
it points at changed after it.

- **Landing evidence**: `tests/test_context.py`:
  `test_lion_context_set_checkpoint_show_restore_and_status_through_the_cli`,
  `test_the_context_routes_answer_for_a_registered_desk_only`,
  `test_the_served_context_routes_sit_behind_the_same_gates`,
  `test_no_model_is_called_by_the_checkpoint_the_restore_or_the_status`; `tests/test_context.py`:
  `test_the_status_is_stale_when_the_file_changed_after_the_take_within_its_second_too`,
  `test_a_prompt_landing_while_the_take_reads_the_transcript_is_after_the_take`,
  `test_a_precompact_hook_takes_the_sessions_checkpoint_from_what_claude_code_hands_it`,
  `test_a_take_refused_by_the_gitignore_writes_nothing_and_an_ignored_one_lands`.

### D4: The resident's arm in `hub/agent/agent.py` ^d4

Serves C1, C6 and C7. When the resident takes and replays is [[ADR-0013-the-agent#^c7|ADR-0013/C7]].
Its take holds the `posture` by its note key, the last wake and the unreported batch by their keys
in the agent's note file, and each value the run declared. Its record is written beside the
checkpoint as `record-<run>.jsonl`, rewritten whole at each take, after the same ignore check. A
take that fails is kept among the agent's errors and the run goes on.

- **Landing evidence**: `tests/test_context.py`:
  `test_the_resident_checkpoints_at_its_fold_and_its_next_process_resumes_the_same_view`,
  `test_the_resident_checkpoints_when_stop_ends_its_run`,
  `test_a_restore_that_cannot_replay_is_said_and_the_run_starts_fresh`,
  `test_a_resident_whose_repository_would_commit_its_checkpoint_takes_none_and_says_so`.

### D5: `hub/backend/codex_checkpoint.py` beside the sources both sessions share ^d5

Serves C1, C3, C7, C8 and C9. A module beside `hub/backend/codex.py`, not a package in its place,
because [[ADR-0009-backends|ADR-0009]] and its tests name that file. The lanes, the pull requests,
the loop file, the fold and the restore's body live in `hub/backend/checkpoint.py`; the Claude Code
module re-exports their names. `lion context checkpoint --codex` reads `--session`'s rollout, else
the directory's newest that no sub-agent wrote; a `--transcript` whose first row is a `session_meta`
is a Codex session's. `restore` renders by `source.kind`.

- **Landing evidence**: `tests/test_codex_checkpoint.py`:
  `test_lion_context_takes_a_codex_session_by_its_flag_or_by_its_rollout_and_restores_it`,
  `test_the_newest_rollout_is_the_directorys_own_thread_and_a_thread_id_names_one_file`,
  `test_a_rollout_is_read_once_per_file_state`,
  `test_a_rollout_handed_to_the_precompact_hook_is_refused_and_writes_nothing`; the Claude Code
  tests in `tests/test_context.py` and `tests/test_claude_code_checkpoint.py`, unchanged.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Continuing the old session with `claude -c` | the whole transcript is read again, and nothing chose what to keep |
| A model's summary as the checkpoint | a model's opinion of the session, unverifiable and unfoldable; a summary is at most one declared value |
| Copying tool calls and their results into the checkpoint | it grows with the session and the fold gains nothing; the transcript keeps them (A1) |
| Re-arming every Monitor that says `persistent` | the tool ends every Monitor at its deadline, and none of 65 persistent ones read was still running (A3) |
| The statusline's last-rendered file as the figure | it belongs to whichever session rendered last; the per-session file is named by the session id (C7) |
| A package `hub/backend/codex/` in place of `codex.py` | the backend's record and its tests name `hub/backend/codex.py`; a module beside it moves no import (D5) |
| Every user message of a rollout read as a prompt | the CLI writes the environment, AGENTS.md and the plugin list as user messages, each item marked (A5) |
| The newest rollout whatever wrote it | a sub-agent runs in its parent's directory and writes as often (D5) |
| One file per directory, overwritten at each take | a take would erase the one before; each take is its own file and `latest.json` names the newest (C6) |

## Consequences

- **S1**: Claude Code's PreCompact hook, when an install sets it to `lion context checkpoint
  --hook`, takes the session's checkpoint with the reason `precompact`. The session, transcript and
  directory come from the hook's input alone; an event of another kind, or one missing a string
  session, transcript or directory, is refused with nothing taken. Nothing else takes one or starts
  the fresh session: a person or another program runs `lion context checkpoint`, starts a session
  and gives it the restore's prompt; `threshold` and `rotate` name such callers.
- **S2**: The statusline's per-session file and the hook ring are written outside the code, by the
  statusline command and the prompt hook a Claude Code install configures; without them the figure
  reads "not known" and the person's words come from the transcript.
- **S3**: `save`, `load` and the restore refuse a reason or a kind outside their lists and name the
  value: a file edited to hold another kind does not load, and a restore leaves no value out unsaid.
  The kind checked is a named value's; `source.kind` is on no list: the restore renders a resident's
  and a Codex session's by it, and `to_history` refuses any other. Pinned by
  `test_save_and_load_refuse_a_why_or_a_kind_off_the_lists_and_name_the_value` and
  `test_the_restore_refuses_a_why_or_a_kind_off_the_lists_and_names_the_value`.
- **S4**: The lanes are the actor's active khive tasks, the threads its unread mail awaits a reply
  on, each named by its newest unread message, and the open pull requests the gh account authored in
  the directory's repository. `source.lanes` names each read, or why it could not be done, and a
  full page says it is one. A thread already read and left unanswered is not a lane. Pinned by
  `test_the_lanes_are_the_active_tasks_the_threads_awaiting_a_reply_and_the_open_pull_requests` and
  `test_a_lane_source_that_cannot_answer_says_why_and_a_full_page_says_it_is_one`.
- **S5**: One `latest.json` serves both arms, so the latest checkpoint that `lion context restore`
  and `status` show is the directory's newest, whichever arm took it. A Claude Code checkpoint taken
  in a resident's directory, the only kind of directory the `/api/context/` routes reach, becomes
  the latest. The resident's next process, loading on a run's kinds, refuses it and starts fresh
  ([[ADR-0013-the-agent|ADR-0013]] S8), its own checkpoint left on disk: the cost is a fresh start,
  never a view the resident did not take. Pinned by
  `test_a_claude_code_checkpoint_in_a_residents_directory_is_refused_by_its_next_process`.
- **S6**: `hub/backend/claude_code/checkpoint.py` reads fixed places under the home directory:
  Claude Code's project directory, the hook ring, the typing log and the statusline's files; where
  no tool writes one, it reads as absent. The script the added inbox Monitor runs, and the actors it
  names after the directory's own, come from the environment (`LION_INBOX_MONITOR`,
  `LION_INBOX_PRIORITY`); unset, the Monitor names `inbox-monitor.sh` on the path of whoever runs
  it.
- **S7**: The code removes no checkpoint: every take adds a file under `.khive/context/`, and each
  resident run's record file stays beside them.
- **S8**: The boundary: the durable record and a session's continuity across processes are the
  kernel's: which checkpoint a new process starts from, who starts it, how long a checkpoint is
  kept, and where it lives once the directory is gone. Here a checkpoint is files in a directory git
  ignores, and a restore is a prompt or a history.
- **S9**: `taken_at` keeps the take's fraction of a second, so `status` calls a file written in the
  take's second stale only when written after it, to the microsecond. A `taken_at` with no fraction
  reads as its second's start. The stamp is read before the transcript is, so a prompt that lands
  while the take reads is after it.
- **S10**: A lane row is an object whose lane id is a non-empty string and whose `properties`, when
  given, is an object; a pull request's number is a positive int, never a bool. A row of another
  shape is counted in `source.lanes` as invalid, the first one named, and never aborts the take; a
  box that is not a list is a read that could not be done, and the khive client refuses such a page
  itself. Pinned by `test_a_row_of_the_wrong_shape_is_counted_invalid_and_never_aborts_the_take`.
- **S11**: `hub/backend/codex_checkpoint.py` reads the rollouts under `$CODEX_HOME`, `~/.codex` when
  unset, at each call. Nothing here takes a Codex session's checkpoint on its own: a person or
  another program runs `lion context checkpoint --codex`. A rollout handed to the PreCompact hook is
  refused, `precompact` being no reason of a Codex session's, and nothing is written. Pinned by
  `test_a_rollout_handed_to_the_precompact_hook_is_refused_and_writes_nothing`.
- **S12**: A Codex checkpoint is saved and loaded on a Claude Code session's lists, which hold a
  Codex session's; its restore checks it on the Codex session's own. Like a Claude Code one (S5), it
  becomes the latest in a resident's directory and the resident's next process refuses it.
