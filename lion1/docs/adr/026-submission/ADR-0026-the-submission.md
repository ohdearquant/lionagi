---
adr: ADR-0026
status: proposed
liveness: partial (an `Actor` is built with a store, the store refuses a config that would put the lion tables in the main store, a chat with no signer refuses to start, and an agent's wake continues its last run with no checkpoint, each pinned by tests; the commands, the driver, the result mail and the MCP server are owed, S1)
date: "2026-10-04"
area: submission
kind: new
depends_on:
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0013-the-agent|ADR-0013]]"
  - "[[ADR-0016-the-lion-command|ADR-0016]]"
  - "[[ADR-0025-the-graph|ADR-0025]]"
tags:
  - adr
  - runtime
  - submission
---

# ADR-0026: The submission

## Context

[[ADR-0025-the-graph|ADR-0025]] S4 names this record: how a caller submits a run of a profile, a
fanout or a named flow, and how the result reaches it. The author's instruction of 2026-10-04 gives
the reason: a coding assistant's own sub-agents are spent too freely, so this runtime takes their
work, reached over its command and over an MCP server, and the sub-agents are turned off once it
does. A flow is then written once and called by name.

Today a run starts in three ways: `lion chat` builds a console actor from flags, `lion agent` builds
one from `chores.toml` and runs on mail, and a test builds an `Actor` directly. None takes a profile
by name, none runs steps, and nothing writes the `steps` table ADR-0025 D1 made. The agent still
takes a checkpoint beside its store (ADR-0025 S9), and an `Actor` still runs without a store
(ADR-0025 S6).

This record fixes the commands, the files they read, the step driver, the result mail, the MCP
server over the same functions, the end of the agent's checkpoint and of the store-less `Actor`.
Source: `apps/cli/lion_cli/main.py`, `apps/cli/lion_cli/chat.py` (`profile`),
`apps/cli/lion_cli/agent.py` (`build`), `hub/agent/agent.py` (`_run_batch`, `forget`),
`src/lion1/store.py`, `khive/pack-lion/sql/08-steps.sql`.

## Definitions

- **submitter**: the actor that asked for a run or a flow: the caller's own, named by the
  directory's `.khive/config.toml`, or the actor the MCP server was started as.
- **profile file**: `profiles.toml` in the actor's directory: `[profile.<name>]`, holding what `lion
  chat`'s flags hold, so a profile is the chat's shape under a name.
- **flow file**: `flows.toml` in the actor's directory: `[flow.<name>]` with its ordered `step`
  array of tables.
- **step**: one table of a flow's `step` array: a name, a profile, a `say` template, and optionally
  `each`, the list it fans out over.
- **flow run**: the run row written for a flow as a whole, under the profile `flow:<name>`; it makes
  no call of its own.
- **step row**: a row of `steps` under the flow run: the step's index, name, profile, the run that
  did its work, what it read, and its outcome.
- **template**: the `say` text with `{name}` holding a variable given at submission, and
  `{step.output}` or `{step.text}` holding an earlier step's result.
- **fanout**: a step with `each`: one run per item of the named list, run together, their outputs
  collected in order under the step's name.
- **result mail**: the message the run's actor sends the submitter when a run or a flow ends: the
  id, the outcome and the output's head.
- **lion database**: the file the actor's config routes the pack's tables to: a backends entry named
  `lion` with a `path`, and `[packs.lion] backend = "lion"`; mail and every other pack stay on the
  actor's main store.

## Assumptions

| id | statement | status | evidence |
| --- | --- | --- | --- |
| A1 | A profile's whole configuration is what `lion chat` already takes as flags: a system text, the tool sets, a model, the bounds. | measured | `apps/cli/lion_cli/chat.py` `profile`, `main.py` `parser` |
| A2 | A run's output is readable from its row alone once it ends: `lion.get(id)` returns the row with `output`. | measured | `tests/test_store.py`, the real-binary test reads a closed run's output back |
| A3 | A caller reads its own inbox: result mail reaches a caller that already sweeps mail, and the MCP tool reads the rows for one that does not. | assumed | the agent of ADR-0013 and the author's console both sweep; the MCP tool is the other path |
| A4 | A session's messages replay as a request's history: `Store.history` returns entries `RunRequest.history` takes, and a run replayed from them continues where the last ended. | measured | `tests/test_store.py`, the replay test under ADR-0025 C3 |

## Claims

### C1: A run or a flow is submitted by name, from a directory, and leaves rows before anything else _(enforced: mechanical; owed, S1)_ ^c1

- **Subject**: `lion run --dir D --profile P --say TEXT` and `lion flow --dir D --name N`, their
  standard output and the store's rows.
- **Violated when**: a call to the model goes out before the run row exists, or the id prints after
  the first call, or a step's id prints after its run opens.

The command builds the actor `D` names, the profile `P` from the profile file or the flow `N` from
the flow file, and runs one job on the model the profile or `--model` names. The id prints first, so
a caller that stops reading still holds the handle; the row exists in the store before the first
call (ADR-0025 C1, C5); the output prints when the run ends. A flow prints its own id, then each
step's as it opens.

- `test_lion_run_prints_the_id_before_the_first_call_and_the_row_exists_then`
- `test_lion_flow_prints_the_flow_id_then_each_steps_as_it_opens`

### C2: Every step is a run of its own, and the flow's run names them all _(enforced: mechanical; owed, S1)_ ^c2

- **Subject**: the step rows under a flow run, and the run rows they name.
- **Violated when**: a step's work has no run row of its own, a step row names no run, a fanout item
  lacks a row, or a step opens after an earlier one failed.

A step's work is one run under the step's profile, with its own session, run, message and call rows,
its trigger the flow run's id. The flow run's step rows name each step's run, in order, and what it
read. A fanout step writes one step row per item, named `<step>[k]`, each with its own run. A step
whose run ends other than in success closes with that outcome, the steps after it do not open, and
the flow run closes `stopped` with the failed step named in its output.

- `test_a_flows_steps_are_runs_of_their_own_named_by_the_flow_runs_step_rows`
- `test_a_fanout_step_writes_one_step_row_and_one_run_per_item_in_order`
- `test_a_failed_step_closes_the_flow_stopped_and_opens_no_step_after_it`

### C3: A step reads only what the template names, and what it names must exist _(enforced: mechanical; owed, S1)_ ^c3

- **Subject**: a step's resolved `say`, and its row's `inputs_from`.
- **Violated when**: a step's text holds an earlier step's result its template did not name, or
  `inputs_from` omits one it did, or a name resolving to nothing leaves a row behind.

A template's `{name}` resolves against the submission's variables and the steps before it, and
nothing else: `inputs_from` lists the steps the template read. A name that resolves to nothing
refuses the flow before its run row is written, naming the step and the name. `{step.text}` is the
run's last text entry; `{step.output}` is its declared output as JSON; a fanout's output is the list
of its items' outputs.

- `test_a_steps_inputs_from_lists_exactly_the_steps_its_template_read`
- `test_a_template_naming_nothing_refuses_the_flow_before_any_row`

### C4: The result reaches the submitter by mail, and the rows hold it regardless _(enforced: mechanical; owed, S1)_ ^c4

- **Subject**: the submitter's inbox after a run or a flow ends, the run row, and `lion result`.
- **Violated when**: a run ends in any outcome and no mail is sent or its send's failure is silent,
  or a row is missing because the send failed, or `lion result` prints less than the row.

When a run or a flow ends in any outcome, the run's actor sends the submitter one message: the
subject carries the id and the outcome, the body the output's head and the command that reads the
rest. A submission that named a thread gets its result on that thread. A send that fails is printed
and kept as an error; the rows are complete without it, and `lion result --dir D ID` prints the run
row with its output, `--history` the session's messages.

- `test_a_run_that_ends_mails_the_submitter_its_id_outcome_and_output_head`
- `test_a_result_mail_that_fails_leaves_the_rows_complete_and_prints_the_error`
- `test_lion_result_prints_the_row_and_with_history_the_messages`

### C5: The MCP server exposes the same three acts and nothing else _(enforced: mechanical; owed, S1)_ ^c5

- **Subject**: the tool list `lion mcp --dir D` serves, and what each tool does.
- **Violated when**: a fourth tool is listed, `run` or `flow` returns after the child ends, the
  child runs on a model the caller named, or a tool sends mail, moves task state or writes a file.

`run` and `flow` start the command of C1 as a child process with the server's actor as the
submitter, and return the id as soon as it prints; `result` reads the rows. The model behind the
tool is the profile's, never the caller's; the caller's own tools are not reached.

- `test_the_mcp_run_tool_returns_the_id_once_printed_and_the_child_runs_on`
- `test_the_mcp_server_lists_run_flow_and_result_and_no_other_tool`

### C6: The agent's wake is a run of the store that continues the last _(enforced: mechanical)_ ^c6

- **Subject**: an agent's run row, its request's history, and `.khive/context/` under its directory.
- **Violated when**: a wake's run row names no `continues` while a last run exists, its history is
  not the chain `Store.history` reads, a run opens whose last run cannot be replayed, or a
  checkpoint file is written or read.

An agent's run opens with `continues` naming the actor's last run from the cursor, whatever profile
ran it, and the history `Store.history` reads from that run's chain ([[ADR-0025-the-graph|ADR-0025]]
C3); nothing under `.khive/context/` is read. This closes ADR-0025 S9. Replayed history is context,
never authority: a reader wake does what its own profile allows. A last run the store cannot replay
refuses the wake until the owner forgets it (C9): no run opens, and the batch is owed to the owner
with the run named.

- `test_a_second_wake_replays_from_the_firsts_rows_and_writes_no_checkpoint_file`
- `test_a_wake_with_no_last_run_continues_nothing_and_reads_no_checkpoint_file`
- `test_each_wake_stores_only_its_own_entries_and_replays_the_whole_chain`
- `test_a_reader_wake_continuing_an_acting_run_cannot_send_whatever_its_history_holds`
- `test_a_cursor_naming_a_run_the_store_does_not_hold_refuses_the_wake_and_names_the_run`

### C7: An `Actor` is built with a store _(enforced: mechanical)_ ^c7

- **Subject**: `Actor.__init__`.
- **Violated when**: an `Actor` exists with no store, or `lion chat` starts in a directory whose
  khive config is missing or names no actor and no `--khive-config` is given.

`Actor` takes its store as a required argument; one built without raises before any run. The
console's actor is built with the store its signer names, so a chat with no signer refuses to start
and says what names one. The tests pass the in-memory double of the pack. This closes ADR-0025 S6.

- `test_an_actor_without_a_store_is_refused_at_construction`
- `test_khive_config_missing_actor_table_or_id_refuses_the_chat_and_names_the_file`
- `test_a_khive_config_that_is_not_a_file_or_names_no_actor_refuses_to_start_and_leaves_no_log`

### C8: A submission's rows live in the actor's lion database, never the home store _(enforced: mechanical)_ ^c8

- **Subject**: the config a `Store` is built under, every command's included, and the binary's
  `database:` line.
- **Violated when**: a store is built while the pack's tables would resolve to the home database, or
  a row of a submitted run is found in the main store.

A store refuses its config unless a `lion` backend with a file's `path` sits beside `main` and
`[packs.lion]` routes the pack there. It refuses two backends of one name, a path SQLite reads as a
URI, and a `lion` path reaching main's file by any name or differing only in case or Unicode form.

The binary is handed that config by absolute name, the directory's own included, without
`KHIVE_CONFIG` or `KHIVE_DB`: the file checked is the file read. Every command that builds a store
inherits the refusal.

- `test_a_store_refuses_a_config_that_would_put_the_lion_tables_in_the_main_store`
- `test_a_store_refuses_a_lion_path_that_reaches_the_main_stores_file`
- `test_kkernel_reads_the_config_the_client_names_never_one_the_environment_names`
- `test_a_residents_notes_are_refused_where_its_config_puts_them_in_the_main_store`
- `test_the_same_run_against_the_built_binary`: the `database:` line names both targets; rows only
  in the lion file; no home database

### C9: Only the owner's forget takes a wake past a run the store cannot replay _(enforced: mechanical)_ ^c9

- **Subject**: the agent's cursor and its `forgotten` record.
- **Violated when**: a wake or rerun gets past a run the store cannot replay without the owner's
  forget, or a forget lets go of a run the store can replay or one the cursor does not name.

Every wake and rerun refuses the same way until the owner forgets that run (`lion agent --forget
RUN`, [[ADR-0016-the-lion-command|ADR-0016]] C1); the forget is kept under `forgotten` with why and
when, and the next wake starts a new chain. Nothing gets past the refusal by itself: a chain that
restarted unasked would lose what the refusal keeps.

- `test_forgetting_the_run_a_refusal_names_lets_the_refused_mail_rerun_on_a_new_chain`
- `test_forget_refuses_a_run_the_store_can_replay_and_any_run_but_the_cursors`

## Decisions

### D1: `lion run`, `lion flow`, `lion result` and `lion mcp` in `apps/cli/lion_cli/submit.py` ^d1

Serves C1, C4, C5 and C8. Four subcommands, each taking `--dir`, the actor's directory; each builds
its store first, which refuses without the lion database (C8). `run` takes `--profile`, `--say`,
`--file` (repeatable; each file's text is an input after `--say`), `--model`, `--var`, `--thread`
and `--notify ACTOR` (default: the directory's own actor). `flow` takes `--name`, `--var`,
`--thread` and `--notify`. `result` takes the id and `--history`. `mcp` takes nothing more. ADR-0016
C1's table and D1 gain these rows in the same change.

- **Landing evidence**: none yet; owed with the code (S1).

### D2: The profile file, read by `profile_from(dir, name, model)` ^d2

Serves C1. `[profile.<name>]` holds `system` (text, or `system_file`), `tools` (names among `files`,
`code`, `shell`, `khive`, `send`), `model`, `box`, `image`, `offline`, and `defaults` (the bounds).
The function builds the `Profile` the way `lion chat` does from the matching flags, so the two share
one builder, `apps/cli/lion_cli/chat.py` `profile`, which gains the keyword form. An unknown key
refuses with its name. A profile is a file read at submission and no entity of the graph; ADR-0025
D1 says so in the same change.

- **Landing evidence**: none yet; owed with the code (S1).

### D3: The flow file and the driver, `hub/flow/driver.py` ^d3

Serves C1, C2 and C3. `[flow.<name>]` holds `output` (the step whose result is the flow's; default
the last) and `parallel` (a fanout's width, default 4). Each table of its `step` array holds `name`,
`profile`, `say` and optionally `each`. `run_flow(actor, flow, vars, backend_for)` opens the flow
run; per step: resolve the template (C3), write the step row, run it as `Actor.run` under its
profile, close the step row; then close the flow run with the named output. A flow is a file read at
submission and no entity of the graph.

- **Landing evidence**: none yet; owed with the code (S1).

### D4: The result mail and `Store.steps` ^d4

Serves C2 and C4. `Store` gains `step_open`, `step_close` and `steps(flow_run_id)` over the pack's
three step verbs, and `result(run_id)`, the row with its output. The mail is one `comm.send` through
the actor's own client, sent from a `finally` after the rows close, so a failing send never loses a
row. Its subject is `run <id> <outcome>` or `flow <name> <id> <outcome>`; its body is the output's
first forty lines and `lion result --dir D <id>`.

- **Landing evidence**: none yet; owed with the code (S1).

### D5: The MCP server, `apps/mcp/server.py` ^d5

Serves C5. A stdio server over the `mcp` package, started by `lion mcp --dir D`. `run` and `flow`
spawn the command of D1 with `--notify` set to the server's actor and return `{"id": ...}` once the
first line prints; the child is started in its own session so the caller's exit does not end it.
`result` calls `Store.result` and returns the row. A caller registers the server in its own MCP
configuration with its directory.

- **Landing evidence**: none yet; owed with the code (S1).

### D6: The agent's run continues the last ^d6

Serves C6 and C9. `Agent._run_batch` passes the cursor's last run as `continues`, with
`history=await store.history(id)`, in its `RunRequest`. A chain the store refuses (`not_found`,
`invalid`) raises `AgentError` before any run, and `Agent.forget` is the owner's way past it; other
store errors propagate. `Store.open` counts a continuing run's history as written, and
`Store.history` walks `continues` oldest first. `_restore`, `_checkpoint` and the threshold
checkpoint go, with their tests; `lion context` stays for the assistant sessions of ADR-0019, whose
readers ADR-0025 C4 keeps. ADR-0013 D3 and the ADR-0019 liveness line say so.

- **Landing evidence**: `tests/test_agent.py`:
  `test_a_second_wake_replays_from_the_firsts_rows_and_writes_no_checkpoint_file`,
  `test_a_wake_with_no_last_run_continues_nothing_and_reads_no_checkpoint_file`,
  `test_a_wake_whose_run_raised_leaves_the_run_it_continued_for_the_next_wake`, C6's three more and
  C9's two,
  `test_a_history_the_store_never_answered_propagates_and_is_neither_refused_nor_forgotten`;
  `tests/test_store.py`:
  `test_a_history_refuses_a_chain_with_a_run_the_store_does_not_hold_or_one_that_loops`.

### D7: `Actor(store=...)` required; the double in `tests/lionfake.py` ^d7

Serves C7. `Actor.__init__` takes `store` without a default and raises `TypeError` with a sentence
naming ADR-0025 D3 when given `None`. Every test that builds an `Actor` takes the `store` fixture
from `tests/conftest.py`, the in-memory pack behind a `Khive` double; `MemoryNoteStore` goes with
the default. `console` in `apps/cli/lion_cli/chat.py` builds the store before the actor and raises
`ValueError` naming the config and `--khive-config` when neither names a signer.

- **Landing evidence**: `test_an_actor_without_a_store_is_refused_at_construction`,
  `test_a_store_refuses_a_config_that_would_put_the_lion_tables_in_the_main_store`, the two chat
  refusals under C7; 207 constructions in the tests take the double; `MemoryNoteStore` is gone.

## Alternatives

- **The submission as a verb of the pack** (a `lion.submit` the store runs): rejected; the store
  keeps state, the runtime runs the model (ADR-0025 S4: the verbs are the state, never the surface).
- **A flow as an LNDL program** (ADR-0008): rejected for now; a flow is a few named steps a caller
  writes in TOML and reads back as rows, with no expressions; a program inside a step is the
  profile's own matter.
- **The result returned on the MCP call** (blocking until the run ends): rejected; a run's length is
  the model's, and a call held open across it is where a caller's own timeout cuts the result away.
  The id returns at once and the mail or `result` carries the rest.
- **A step reading every earlier step** (the whole flow as one history): rejected; a step reads what
  its template names, so `inputs_from` is a fact and a fanout's items do not read each other.

## Consequences

- **S1**: Owed: C1 to C5, each with the tests named there; C6, C7 and C8 have landed. Landing order:
  D2 to D4 with `lion run`, `lion flow`, `lion result`, then D5.
- **S2**: [[ADR-0016-the-lion-command|ADR-0016]] C1's table and D1 gain the four subcommands in the
  change that adds them. [[ADR-0013-the-agent|ADR-0013]] D3 is amended: the agent's history is its
  last run's session, and its cursor names that run.
- **S3**: [[ADR-0025-the-graph|ADR-0025]] S1's flow driver, S6 and S9 are closed by this record; its
  liveness line changes when they land. Its D1 no longer awaits a record that writes a profile or a
  flow to the graph: this one writes neither, and D1 says the pack registers no entity type.
- **S4**: The profile file and the flow file are read at submission and never cached: an edit takes
  effect on the next submission.
- **S5**: A fanout's width is the flow's `parallel`; nothing yet bounds it across flows or against
  the day's cost ([[ADR-0013-the-agent|ADR-0013]] caps apply to an agent's wake, not a submission).
  A submitted run's row carries what finds its process (the host and the pid, in `output` until a
  column exists); a `lion stop ID` is owed; both it and a bound across flows land before this
  surface replaces a caller's sub-agents everywhere.
- **S6**: The MCP server's `run` tool is the only path by which a caller's assistant reaches this
  runtime; the server offers no tool of the caller's own to the model, so a profile's tools are the
  whole of what a submitted run can do.
- **S7**: Owed: C8's same-file check also runs where the files are opened. The refusal compares
  paths before anything exists to open, and each spelling it does not foresee reaches main's file
  past it (case or Unicode form, a firmlink over a file not yet made, a URI). Once both backends are
  open, the two files' device and inode are compared, and the lion pack refuses to serve when they
  are one file. The config-time refusal stays: it names the mistake before any row is written.
- **S8**: Nothing yet bounds a continuation: an agent's chain grows by one run per wake, and each
  wake replays the whole of it. A fold point, after which a wake starts fresh from its cursor and
  posture, is owed beside S5. Until it lands, and until the agent's directory routes the lion tables
  (C8), no agent is served from this runtime.
