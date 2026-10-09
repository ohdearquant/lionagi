---
adr: ADR-0025
status: proposed
liveness: partial (the pack builds and serves seventeen verbs; a field past one argument rides as blob pieces under a verb of its own, a session reads a page at a time and a write that failed part way is finished by seq; the core writes every row of a run and replays one from them, and a harness session rides the same tables, each pinned on a stub binary and on the built one; the flow driver of ADR-0026 writes the steps rows, pinned the same way; the pack's own tests are owed, S1)
date: "2026-10-04"
area: graph
kind: new
depends_on:
  - "[[ADR-0007-the-record|ADR-0007]]"
  - "[[ADR-0013-the-agent|ADR-0013]]"
  - "[[ADR-0019-the-checkpoint|ADR-0019]]"
tags:
  - adr
  - runtime
  - graph
---

# ADR-0025: The graph

## Context

This record serves the author's instruction of 2026-10-04. The runtime's state goes into the store,
as a hard dependency. A run, its messages and its model calls are rows the store owns, read by id,
never embedded, and a call row repeats no input. The file-backed note store goes. A run restores by
replaying its session in full, so no checkpoint is taken for it.

Today a run's record lives in the process ([[ADR-0007-the-record|ADR-0007]]), a checkpoint writes a
folded view of it to a directory ([[ADR-0019-the-checkpoint|ADR-0019]]), and the agent keeps its
cursor in a note file ([[ADR-0013-the-agent|ADR-0013]]). A Claude Code or Codex session's history is
read back from the CLI's own transcript by the checkpoint's scanners. Nothing can list the runs of
an actor, read a run's history from outside its process, or search what a model was told.

This record fixes the vocabulary: which things are rows in tables of the runtime's own pack, their
fields, and that none of them is an entity on the graph. It fixes who writes them and that a run
restores by replaying its session. The pack that owns the tables is D4. Source:
`src/lion1/record.py`, `src/lion1/actor.py` (`Run`), `src/lion1/checkpoint.py`,
`src/lion1/khive.py`, `src/lion1/store.py`.

## Definitions

- **store**: the actor's khive, reached as [[ADR-0001-the-actor|ADR-0001]] reaches it.
- **session row**: a row of the pack's `sessions` table: one history, the entries a model was shown
  and what it said, in order.
- **message row**: a row of `messages`: one entry of a session; for a run of this runtime, one
  `Entry` of its record.
- **call row**: a row of `calls`: one request to a model and its answer, or its refusal.
- **run row**: a row of `runs`: one job of an actor under a profile, with its session and its
  outcome.
- **replay**: building a run's history from its session's messages, in order, with their directives
  applied.
- **harness session**: a session whose source is a CLI the runtime drives, read from that CLI's
  transcript.
- **pack**: the crate that owns this record's tables and serves its verbs.

## Assumptions

| # | statement | source | if false |
| - | --------- | ------ | -------- |
| A1 | A pack built in this repository against a pinned store revision can own tables and serve verbs over them. | a sibling project builds two such packs and a binary this way; the store's own session pack declares its tables as a schema plan | the rows live in a file of the runtime's own until the store takes them |
| A2 | A session's messages, each carrying the directive its entry carried, fold to the same view the live run had. | `fold` reads directives from entries (`directives` in `record.py`); a checkpoint's `to_history` replays entries the same way | a run resumed from the store starts from a different view, and a checkpoint stays |
| A3 | One store call per turn for the entries, one per call open and close, keeps up with a run. | measured 2026-10-04: two runs of two and one turns made 21 calls of the store's binary | the writes move to one call per turn |

## Claims

### C1: Every entry is a message in the store before the next call _(enforced: mechanical)_ ^c1
- **Subject**: every `Entry` a run appends, and every call the run makes after it.
- **Violated when**: a call is made while an entry appended before it has no message row.

The runtime writes a message for each entry at the time it is appended, with the entry's sequence
number, kind, alias, turn, text and the directive the entry carries, if any. A write that fails
stops the run with the store's error (Refused), since a history the store does not hold cannot be
replayed. A test appends entries, kills the run between two of them, and reads back every entry
written before the kill (S1).

### C2: Every call is recorded, answered or not _(enforced: mechanical)_ ^c2
- **Subject**: every request the runtime sends a model.
- **Violated when**: a request has no call row, or a call row names a reply that is not a message.

A call is written when the request is sent, with the model and the sequence numbers rendered or
folded. It is completed when the answer or the refusal arrives, with the reply's sequence number (a
refusal is a marked message), the tokens, the cost and the duration. A cut call has no reply; a
request that raised closes the call `failed` with its error and the run `stopped`. The reply's row
lands with the next flush (C1): the pack checks the status, a test that each reply is a row.

### C3: A run restores by replay, from its session and the chain it continues _(enforced: mechanical)_ ^c3
- **Subject**: every run resumed from the store, by its session row and each run row's `continues`.
- **Violated when**: a resumed run's first view differs from its predecessor's at its last call, a
  resume reads anything but the messages on that chain, or a continuing run writes what it replays.

The messages of the sessions its `continues` chain names, oldest first and folded by their
directives, are the resumed run's history, with the predecessor's bounds and folding. A continuing
run writes only its own entries, so the session plus its chain is the whole of what a resume needs.

A model value replays as its JSON object, as [[ADR-0019-the-checkpoint|ADR-0019]]'s log does; that
record's checkpoint is not taken here (S2). A chain naming a run the store lacks, or one that loops,
refuses the resume. Tests compare a resumed job's views entry by entry (S1) and count seven rows per
wake over five ([[ADR-0026-the-submission|ADR-0026]] C6).

### C4: A harness session is a session in the same tables _(enforced: mechanical)_ ^c4
- **Subject**: every Claude Code or Codex session the runtime drives.
- **Violated when**: its turns are readable only from the CLI's transcript file.

The scanners of [[ADR-0019-the-checkpoint|ADR-0019]] read the CLI's transcript into exchanges. The
session row names the CLI as its source and the CLI's own session id as its provider session id, and
keeps the transcript's path. Each exchange is one `user` row of kind `exchange`, its `raw` the
recorded exchange (`{turn, at, input, text, kind, why}`) and its `text` the prompt.

A take appends only the exchanges whose seq the store does not hold (D10), so taking the same
session twice writes nothing, and the restore folds them back. The functions `record_exchanges`,
`latest_session` and `exchanges_of` (`hub/backend/checkpoint.py`) write and read them, pinned on the
stub binary by the context tests and on the built one by
`test_a_harness_session_rides_the_same_tables_against_the_built_binary` (`tests/test_store.py`).

### C5: A run names its trigger, its session and its outcome _(enforced: mechanical)_ ^c5
- **Subject**: every run, including an agent's wake.
- **Violated when**: a run row lacks the inbound ids that started it, its session, or, once ended,
  one of the five outcomes with its cost and turn count.

The run row is written before the first call with its actor, profile, bounds, trigger and session
row, and completed at its end. A wake of [[ADR-0013-the-agent|ADR-0013]] is a run whose trigger is
the mail batch; the cursor note is replaced by it (S3). A test reads the run of a wake and finds the
batch's ids on it (S1).

### C6: A profile's notes are rows of the store, and nothing else keeps them _(enforced: mechanical)_ ^c6
- **Subject**: every note a run writes under `note.` ([[ADR-0007-the-record|ADR-0007]] C5), for
  every profile of an actor that has a store.
- **Violated when**: a note is read from or written to a file, or a note written by one run is not
  read back by the next run of the same profile from the store.

A note is one row per profile and key, with its value, the run and seq that wrote it and its
version, its write's time. `get`, `put`, `list`, `find` and `delete` are the pack's verbs; `find` is
a substring match over key and value, folding ASCII case only ([[ADR-0007-the-record|ADR-0007]] D6).
The file store is removed; the memory store stays for tests. A test writes a note in one run and
reads it in a second run of the same profile through the store alone (S1).

### C7: A message of any size is written whole and read back whole _(enforced: mechanical)_ ^c7
- **Subject**: every message row a run or a harness take writes, and every read of those rows by
  this runtime: `Store.messages`, `Store.history` and `exchanges_of`.
- **Violated when**: a row's `text`, `raw` or `directive` is cut or stored without its content, a
  write is refused for the size of one of them, or one of those reads cannot return a session for
  its total size.

The binary takes its ops as one argument, which Linux bounds at 128 KiB a string and the store's
parser at 1 MiB. The store's daemon answers in frames of at most 8 MiB, and stops starting a call's
ops once their answers pass 4 MiB.

So a large field travels as pieces in the blob store (D8, D11), a session is read a page at a time
(D9), and a write that failed part way is finished by seq (D10). A row's other arguments still
travel inline (S16).

## Decisions

### D1: Five tables beside the graph, and no entity yet ^d1

Serves C1 to C5. The pack (D4) owns five tables and registers no entity type: a profile and a flow
are files read at submission ([[ADR-0026-the-submission|ADR-0026]] D2, D3), nothing of the graph. A
row is not a note: it is read by its id, never embedded, indexed or linked. The actor is the
namespace, no entity.

| name | class | fields |
| ---- | ----- | ------ |
| `sessions` | table | id, source, provider_session_id, actor, profile, started, ended, message_count, transcript |
| `messages` | table | session_id, seq, role, kind, turn, alias, text, raw, content_hash, parent_seq, directive, carried |
| `calls` | table | id, session_id, turn, model, seq_from, seq_to, folded, reply_seq, tokens_in, tokens_out, cost, duration_ms, status, error |
| `runs` | table | id, actor, profile, session_id, bounds, trigger, outcome, cost, turns, started, ended, output |
| `steps` | table | flow_run_id, index, name, profile, run_id, inputs_from, outcome |

A call row holds no content: `seq_from`, `seq_to` and `folded` name its messages, a summarised one
folded. `text` is the entry's content, a result's as JSON; `parent_seq` and `tokens_out` are null so
far.

- **Landing evidence**: `khive/pack-lion/sql/01-sessions.sql` to `10-notes.sql`, one file per table
  and index; `khive/pack-lion/src/vocab.rs` (no entity type);
  `tests/test_store.py::test_a_run_writes_its_rows_and_a_second_run_replays_them` reads every row
  back by id.

### D2: Keys, not edges ^d2

Serves C3 and C5. A message and a call belong to their session by `session_id`; a run names its
session the same way; a step names its flow's run and its own run; a run resumed by replay carries
the id of the run it continues. Order is `seq` and `index`. Nothing in these tables is an edge of
the graph; a graph record that needs a run points at it by id in a property.

- **Landing evidence**: `tests/test_store.py::test_a_flows_step_rows_against_the_built_binary`: each
  step row names its flow's run and its own run, in order, and each step's run names the flow's run
  as its trigger; `continues`, under C3's replay test.

### D3: The core writes, as the actor, through its own client ^d3

Serves C1, C2 and C5. The store client moves into the core (`lion1.khive`), and `lion1.store` writes
the rows: the session and the run at the start, every entry not yet written before each call, the
call around each request, the outcome at the end. The core gains no package: the client starts the
store's own binary. A harness session is written by the scanner that reads its transcript. Every
actor is built with a store ([[ADR-0026-the-submission|ADR-0026]] C7, closing S6).

- **Landing evidence**: `tests/test_store.py`: the run's rows and the call rows closed `ok` with the
  reply's seq and price (`…_and_a_second_run_replays_them`), a refusal closed `refused` with its
  reason (`test_a_refused_reply_closes_its_call_refused_with_the_reason`), an abandoned call closed
  `cut` (`test_a_cut_call_closes_its_row_cut_and_the_run_goes_on`).

### D4: The pack, in this repository, with its own verbs ^d4

Serves C1 to C5. A crate under `khive/` declares D1's tables as its schema plan and serves the verbs
below, built against a pinned revision of the store and linked into a binary that is the store's CLI
with the pack in it. The store's own code is not changed; what the store should take over is decided
once this runtime has run on these verbs (S5). Each table's DDL and each verb's query is its own
file under the crate's `sql/`, pulled in at compile time.

| verb | does |
| ---- | ---- |
| `lion.session_open`, `lion.session_close` | start a session row; end it with its count |
| `lion.message_append` | one message row, its fields inline; refused when its `seq` is taken or it names an argument the pack does not know, `carried` included |
| `lion.message_append_carried` | one message row whose large fields are pieces (D8); refused as above, or when a piece is missing or misshapen (D11) |
| `lion.call_open`, `lion.call_close` | a call row at the request; its answer or refusal |
| `lion.run_open`, `lion.run_close` | a run row before the first call, its step row with it; the outcome of both |
| `lion.get` | one row of any table by id; a session's messages, a flow run's steps, in order; refused for a session holding a carried row |
| `lion.messages` | a session's messages after a seq, a page at a time (D9) |
| `lion.runs` | runs newest first, by actor |
| `lion.sessions` | sessions newest first, by source and by actor when either is named |

- **Landing evidence**: the binary builds under `khive/` and
  `tests/test_store.py::test_the_same_run_against_the_built_binary` runs the run on it when
  `LION1_KHIVE` names it; the other tests there run it on a stub serving the same verbs
  (`tests/lionfake.py`); `test_the_store_refuses_as_the_pack_does_and_the_client_names_the_kind`.
  The pack's own tests are owed (S1).

### D5: Replay replaces the checkpoint for runs of this runtime ^d5

Serves C3. `lion context checkpoint` keeps working for harness sessions until C4 lands, and is then
a read of the store. The checkpoint's named vars come from the store's notes at wake time as they do
now; the folded view is recomputed by `fold` over the replayed messages.

- **Landing evidence**: `Store.history` and the second run of
  `test_a_run_writes_its_rows_and_a_second_run_replays_them`, whose entries and `continues` are read
  back; no checkpoint is taken for a run with a store.

### D6: The notes table and its five verbs ^d6

Serves C6. `lion_notes` holds namespace, profile, key, value as JSON, run, seq, version and the
write's time, keyed by profile and key. `lion.note_put` writes or replaces a row and counts its
version up; `lion.note_get`, `lion.note_list` (by key prefix, in key order), `lion.note_find`
(substring over key and value, ASCII case folded) and `lion.note_delete` read and remove. The core's
`KhiveNoteStore` is the five-method store over them, one call of the binary per method.

- **Landing evidence**:
  `tests/test_store.py::test_the_note_store_reads_and_writes_rows_and_folds_ascii_case`,
  `tests/test_actor.py::test_the_khive_note_store_refuses_what_json_cannot_carry_before_any_call`,
  `tests/test_notes.py::test_note_read_reads_another_profiles_rows_and_keeps_no_store_for_it`; the
  agent's readers: `hub.areas.agent_notes`, pinned by `tests/test_areas.py`, `tests/test_daemon.py`,
  `tests/test_instruments.py` and `stats` in `tests/test_agent.py`.

### D7: One `exec`, and one method for the mailbox ^d7

Serves C1 to C6. The client is `Khive.exec(ops)`: a verb's call is written at its call site as the
DSL, not wrapped in a Python method per verb. Two mailbox methods stay, each for a reason of its
own. `inbox` slices a long poll under the daemon's deadline and reads a lost answer once again.
`send` is where the agent's boundary checks the recipient in code. The mark-read is written where
the agent takes its mail, as `comm.read(ids=...)`. The file note store and `notes_dir` go with D6.

- **Landing evidence**: `tests/test_khive_client.py` (the client has no `mark_read`); `exec_sync` in
  `tests/test_store.py::test_exec_sync_times_out_and_refuses_output_it_cannot_parse`; no module
  imports `FileNoteStore` or passes `notes_dir`.

### D8: A field past one argument goes to the blob store as pieces ^d8

Serves C7. A message's `text`, `raw` or `directive` whose DSL literal passes 40 KiB is written as
pieces, one `blob.put` each, every piece but the last exactly 90 KiB; no upload stays open, so no
daemon is needed. The row goes by `lion.message_append_carried`, each carried field empty, and
`carried` holds one manifest per field: its size and its refs in order, or past 256 refs an index,
the refs as hex text carried the same way. No size is refused.

- **Landing evidence**: `tests/test_carried.py`, double and built binary: entries and exchanges past
  one argument read back whole, an indexed field, a 128 KiB directive beside a 128 KiB text, a
  character a join cuts.

### D9: A session is read a page at a time ^d9

Serves C7. `lion.messages` returns a session's rows after a seq, stopping before the rows' JSON
passes 2 MiB, the first row always, with the last seq while rows remain. Every row this runtime
writes fits one argument, so a page fits the daemon's frame. `Store.messages`, `Store.history` and
`exchanges_of` read every page and refuse one that does not move, then fetch each distinct payload
piece once, 30 to a call, and check each field's size (S20). A take reads seqs only. `lion.get` on
`messages` refuses a session holding a carried row.

- **Landing evidence**: `tests/test_carried.py`, double and built binary: a session past 2 MiB read
  in pages, 70 pieces in three reads, `lion.get` refused for a carried session; on the double, a
  counting take fetches no piece.

### D10: A write that failed part way is finished by seq ^d10

Serves C1, C4 and C7. A call's ops run concurrently, so after a failure or a lost answer, which rows
stand is unknown. A take appends the seqs the store does not hold, never resuming from a count, and
compares none it holds (S19). A run is marked unsure while a flush runs; the next flush reads back
whole each held row it would send, refuses one that differs from that row as the pack keeps it, and
sends the rest. Pieces of a row never stored stay behind (S12).

- **Landing evidence**: `tests/test_carried.py`: a take after a gap writes only the missing seq; a
  flush whose answer was lost is finished by the next, and one refuses a held row that differs.

### D11: The pack verifies every piece, and neither verb takes the other's row ^d11

Serves C7. The pack reads each piece back verified against its ref. A missing piece, a piece whose
size is not its place's, refs that do not match the size, an index of the wrong size, or bytes that
are not UTF-8 across the joins refuse the row. A carried text is hashed as it is read, and its
column holds a marker naming its size (S18). `lion.message_append` refuses `carried` and any unknown
argument; an older pack lacks the carried verb. No row is stored without its fields.

- **Landing evidence**: `tests/test_carried.py`, double and built binary: each refusal by name, the
  inline verb refusing `carried`, the marker; on the built binary, an unknown argument refused.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Writing into the store's own session pack tables | they are another pack's schema and its mirror's; a column change there breaks this runtime, and the author keeps the store untouched |
| The whole transcript as one note per run | nothing can read one turn, search one message or resume from the middle |
| Keeping the checkpoint as a kind | a session replays in full, so a folded copy is a second source that can disagree with the first (C3) |
| Call figures on the assistant message | a refused or limited call has no assistant message, and would have no row (C2) |
| The actor as an entity | the store already names it as the namespace of every row |
| Sessions, messages, calls and runs as notes on the graph | every row would be embedded and indexed for a log that is only ever read by id, and the call rows would be a second copy of the messages |
| Notes as records on the graph | a note is read by key and found by substring, by one profile; a graph record would be embedded and ranked for a lookup that never ranks, and its body is replaced without a version |
| The store's generic verbs over the tables | they reach notes and entities only; a table of the pack's own takes the pack's own verbs |
| The CLI's transcript as the harness session's source of truth | it is one file on one machine that the CLI rewrites, and the runtime cannot search it |
| A large field inline, through the binary's ops file | the binary writes it in its own process beside the daemon, and a row past 8 MiB cannot be read back through the daemon (measured) |
| One staged upload per field, read back in ranges | the upload's id lives in the daemon's memory, so a process with no daemon cannot write, and each range reads the whole object on the store's side |
| The pieces' refs in a table of their own | a second write per row and a join on every page, where a column the pack adds to its existing table carries the same list |
| The client sends a carried text's hash | the hash is the pack's own statement of what it holds, and the pack reads the pieces to verify them anyway |
| `lion.get` on messages paged by default | a reader that does not page would take part of a session for the whole |
| A cap on a carried field's size | a row has no size bound, an exchange is recorded whole (C7); and a literal grows to six or twelve bytes a character, so a cap bites far below its stated size |
| `carried` as an argument of `lion.message_append` | a pack that predates it ignores the argument and stores the row with its fields empty (measured at the pinned store revision) |
| Carrying `text` and `raw` only | the directive travels as its own argument as well as inside `raw`, and a summary's directive alone can pass one argument |
| A take resuming from the count of rows held | a call's ops run concurrently, so a later row can stand where an earlier one failed; the count then skips the missing row and collides with the standing one |

## Consequences
- **S1**: Owed: the pack's own tests in Rust, landing with tests named here. The flow driver that
  writes `steps` rows landed with [[ADR-0026-the-submission|ADR-0026]] D3; D2's evidence reads its
  rows back from the built binary.
- **S2**: [[ADR-0019-the-checkpoint|ADR-0019]] is superseded for runs of this runtime: a run with a
  store takes no checkpoint and restores by D5; its liveness line says so in the same change. Its
  scanners stay as the readers of C4.  The served actor's resume
  ([[ADR-0021-the-served-actor|ADR-0021]]) loads a run's history from the service's own log; with a
  store it becomes `Store.history`, the log staying the service's record (owed). The host
  ([[ADR-0022-the-app-and-its-host|ADR-0022]]) reads a resident's notes through
  `hub.areas.agent_notes` (D6) and changes nowhere else.
- **S3**: [[ADR-0013-the-agent|ADR-0013]] D3 is amended: the agent's notes are rows of the store.
  The cursor note gives way to the run row when the agent's wake is a run of this store (owed).
- **S4**: How a caller submits an agent run, a fanout or a named flow, and how the result reaches
  it, is the next record. The `lion` command ([[ADR-0016-the-lion-command|ADR-0016]]) gains a run of
  a profile and a run of a flow by name, each writing D1's run and step rows and notifying the
  submitter by mail. A caller adds the binary on its path and its actor's config. The verbs are the
  state those commands write, never the surface a caller uses.
- **S5**: Reconciling these kinds with the store's own session pack is the store's later decision,
  taken once this runtime has run on them.
- **S6**: Closed: the store was optional on `Actor` while the callers and the tests moved to it, a
  departure from the instruction under its own one-step allowance;
  [[ADR-0026-the-submission|ADR-0026]] C7 made it required, and the tests take an in-memory double
  of the pack. The note store's methods are synchronous and each starts the binary; an asynchronous
  store is a later change.
- **S7**: `khive/Cargo.toml` pins the store's revision. The binary of this repository and the
  store's own open one database, so the owner of this repository bumps the pin in the same change
  that moves the store's binary. A check outside this repository compares the revision each binary's
  `--version` names and exits non-zero on a difference. Under `LION1_KHIVE` the store's tests run on
  the built binary, against the backends their own config names (S10).
- **S8**: `hub.areas.agent_notes` reads a resident's notes in one call of the binary; the host's
  pages make one per resident per read. A batched read across residents is owed.
- **S9**: Closed: the agent of [[ADR-0013-the-agent|ADR-0013]] passed a store and still took its
  checkpoint and restored from it, naming its wake's mail as the trigger, a departure from the
  instruction under the same one-step allowance as S6. [[ADR-0026-the-submission|ADR-0026]] C6 made
  its wake a run of this store that continues the last, and a test there wakes an agent twice and
  finds the second run replayed from the first's rows with no checkpoint file written.
- **S10**: The binary's default database is the one under the home directory; `[storage]` in a
  config file names none (a backends table does, and `KHIVE_DB` cannot join it). Every run of the
  real binary from this repository's tests or scripts names its database and asserts, from the
  binary's own `database:` line and the rows it reads back, that the named file is the one written,
  for every kind the pack writes.
- **S11**: Blob objects carry no namespace in the store: a piece is readable by any actor of the
  same store that knows its ref, a BLAKE3 hash of its bytes. The row naming it is read under its
  namespace, as every row is.
- **S12**: Pieces written for a row whose append then fails stay in the blob store with no row
  naming them; nothing in this runtime removes them.
- **S13**: Owed: a run's `output` (`lion.run_close`), a note's value (`lion.note_put`) and a mail's
  content still travel inline in one call and are refused past one argument.
- **S14**: Only this repository's binary serves the pack's verbs; the store's own binary on this
  machine lists no `lion` pack (measured 2026-10-09). Serving them beside the store's own daemon
  needs a socket and a store of their own, which is the store owner's open decision.
- **S15**: An actor whose config loads `lion` loads `blob` beside it. The store's default packs
  include `blob` and leave out `lion`, so a config that names its packs names both; without `blob`
  the binary refuses every carried write as an unknown verb, by name (measured on the built binary).
- **S16**: A row's arguments other than its `text`, `raw` and `directive` travel inline. Each writer
  weighs every row's call, by either verb, and closes a call before its string passes 131071 bytes.
  A row whose own call passes it, which only an alias or a session id that long can cause, is
  refused by name before any piece, after the rows before it are written. Neither is a message's
  content.
- **S17**: A row written by another client through the binary's ops file can pass one argument, and
  one past 8 MiB cannot be read through the daemon. This runtime writes no such row (D9); reading
  one is the store owner's open question, with S14. A run's own row is read whole for its chain, and
  its `output` is bounded by one argument (S13).
- **S18**: Rows this runtime writes carry `raw` whenever they carry `text`. Against this pack, a
  client older than this change is refused at `lion.get` for a session holding carried rows. A
  binary older than this pack serving such a store, as a rollback would, shows a carried text's
  marker in a listing, and an older client's replay and checkpoint readers refuse the row's null
  `raw`. No older code can be made to refuse more, so serving a store holding carried rows from an
  older binary is unsupported (S7).
- **S19**: Owed: a take places each exchange by its position in the transcript's scan and compares
  nothing the store holds. A scan that changed before a held seq (a rewound session, or a scanner
  that now admits or splits an earlier prompt) shifts the later exchanges, and the store keeps its
  own at each held seq beside the new scan's past them. A shrunk scan writes nothing. Refusing any
  difference would also refuse sessions a scanner change shifts, so what identifies an exchange
  needs its own decision.
- **S20**: A reader holds the session it returns in memory. While a field is assembled it holds its
  pieces and its joined bytes, two copies of its size, and its decoded text at one to four bytes a
  character: three to six times its size. An index is read once per field; the pack holds one
  payload piece at a time, and an index whole.
