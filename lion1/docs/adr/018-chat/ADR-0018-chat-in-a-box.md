---
adr: ADR-0018
status: draft
liveness: operating (the chat, its resume and the boxed chat are pinned by tests against scripted backends and a stubbed box; the one real-box test runs only when enabled; no test interrupts a boxed chat)
date: "2026-09-25"
area: chat
kind: new
depends_on:
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0003-the-bounds|ADR-0003]]"
  - "[[ADR-0005-command-handling|ADR-0005]]"
  - "[[ADR-0007-the-record|ADR-0007]]"
tags:
  - adr
  - console
  - chat
---

# ADR-0018: Chat in a box

## Context

A person wants to work with a model over a directory at a terminal: ask, watch it read and run
things, correct it, stop, and pick the same conversation up later. The loop has everything a
conversation needs except the person. This record fixes what a chat is, which tools and guidance it
gets under which flags, where its work lands, how work done in a remote copy comes home, and what a
later session resumes.

A chat is one run ([[ADR-0002-the-run|ADR-0002]]) whose job carries a `wait` and a `history`
([[ADR-0003-the-bounds|ADR-0003]]); its record and its fold are those of
[[ADR-0007-the-record|ADR-0007]], and its directory's hooks run as
[[ADR-0005-command-handling|ADR-0005]] says. The box, the trees the tools work and the executor that
isolates them are the kernel's concern, reached through [[ADR-0011-the-box|ADR-0011]] (the box). The
chat is a console over the loop and decides none of them. Source: `hub/chat.py`; `console`,
`prepare_box`, `take_patch`, `unsettled` and `chat_main` in `apps/cli/lion_cli/chat.py`; the `chat`
parser in `apps/cli/lion_cli/main.py`; `Console` in `apps/cli/lion_cli/view.py`; `hub/guidance/`.

## Definitions

- **chat**: one conversation between a person at a terminal and one actor over a directory: one run,
  one record, one chat log, and one box or none.
- **console**: the actor and profile `lion chat` builds for a directory: the tools and the guidance
  its flags select.
- **chat log**: `.lion/chats/<id>.jsonl` under the directory by default: every record entry as one
  JSON line, written as it lands, with a usage row per backend call between them.
- **`wait`**: the job field that makes a run a conversation: called at a turn's start after a turn
  with no command, or after a pause, it puts the person's next line on the actor's inbox, or ends
  the run `Stopped`.
- **`history`**: the job field holding an earlier run's entries, replayed onto the new record before
  its inputs.
- **boxed chat**: a chat under `--box daytona`: the directory pushed into a remote box at its own
  path, and every tool working that copy.
- **baseline**: the commit made in the box's copy right after the push, of what the push archived.
- **chat patch**: the copy's diff against the baseline, saved as `.lion/chats/<id>.patch` under the
  chat log's id.
- **unsettled command**: a command a logged turn asked for whose alias has no RESULT in the log.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | The files git lists, tracked ones and untracked ones not ignored, are the directory the person means; `.git` is never among them. | `DaytonaSandbox.push` lists with `git ls-files --cached --others --exclude-standard`; every file when the directory is not a repository | the copy lacks a file the work needs, or carries one that should have stayed home |
| A2 | The copy's diff against the baseline, committed, staged, unstaged and untracked, is the whole change the model made. | `git_diff` in `hub/tools/box.py`; `test_baseline_diff_keeps_committed_and_uncommitted_edits` | a change never reaches the person, and the chat says there was none |
| A3 | `git apply` checks every hunk before it writes one. | git; `test_a_patch_that_will_not_apply_is_saved_whole_and_lands_nothing` | `--apply` can land half a patch in the person's tree |
| A4 | One session fits one run's turn budget, 200 by default. | `--rounds` in the parser | the chat ends `Exhausted` mid-work and the person continues it with `-c` |

## Claims

### C1: A chat is one run that waits for the person, logged entry by entry _(enforced: mechanical)_ ^c1

- **Subject**: every call to `hub.chat.chat`.
- **Violated when**: a line the person types starts a second run, an entry reaches the log twice or
  out of order, the log lacks an entry of the record, or an `OUT{}` is accepted.

The first line is the job's input; each later one lands on the same record as inbound INPUT from
`console`. A turn with no command hands the conversation back, and `wait` asks for the next line; a
turn with commands runs them first.

After the first line, `/quit`, `/exit`, `/q` or the end of input ends the run `Stopped`. The chat's
`accept` refuses every `OUT{}`; three in a row end the run `Refused`
([[ADR-0003-the-bounds#^c4|ADR-0003/C4]]). Each entry reaches the log as it lands, and the record is
flushed whole however the run ends. A first Ctrl-C lets the turn in flight settle, then hands back;
a second, or one at the prompt, ends the chat.

### C2: The flags choose the tools and the guidance _(enforced: mechanical)_ ^c2

- **Subject**: `console()` and the flags of `lion chat`.
- **Violated when**: a tool the flags did not select reaches the model, a boxed chat gets the VM's
  guidance, or `khive_ops` appears where no khive actor is configured.

Every chat gets `read` and `list_dir` over its tree, and the context and note commands. `khive_ops`
comes when the directory's khive config names an actor, bounded to an allow list, sending to no one.
`--shell` adds `bash` and `python` in a local VM mounting the directory at its own path; `--offline`
cuts that VM's network. `--code` adds `read_lines`, `search` and `edit`, and the VM when there is no
box. In a boxed chat `bash` and `python` run in the remote box, and the file and code tools work its
copy.

The guidance is `hub/guidance/console.md` holding `sandbox.md` for the VM or `box.md` for the box,
then with `--code` `code.md`: the root, the branch and the root's `AGENTS.md` and `CLAUDE.md`.

### C3: Without a box the tools work the person's tree; with one, a copy _(enforced: mechanical)_ ^c3

- **Subject**: the tree the file, code and shell tools read and write.
- **Violated when**: a file, code or shell tool in a boxed chat writes on this machine, or a tool
  path with `..` or another absolute path reaches outside the tree.

Without a box the file and code tools work the directory itself, and the VM, when there is one,
mounts it: an edit is a write in the person's tree, and the guidance says so. Under `--box daytona`
the directory is pushed into the box at its own path, the tools read and write that copy, and the
guidance says the person's files do not change.

On this machine a boxed chat writes its chat log, the profile's note file under `.lion/notes/`, the
chat patch at the end and, with `--apply`, the patch's changes. The directory's own hooks run here
as configured ([[ADR-0005-command-handling#^d3|ADR-0005/D3]]). The local tree also refuses a symlink
that leaves it.

### C4: The copy's diff comes back printed and saved, and `--apply` lands it whole or not at all _(enforced: mechanical)_ ^c4

- **Subject**: the end of every boxed chat.
- **Violated when**: a changed copy yields no chat patch, an edit the model committed in the box is
  missing from it, the patch is not taken before the box stops, or a patch that does not apply
  changes the directory.

After the push the copy gets a repository of its own when it has none, and the baseline commits what
the push archived, a tracked file that `.gitignore` also matches included. At the end, before the
box stops, the diff against the baseline is printed and saved; untracked bytecode stays out, and an
unchanged copy saves nothing.

Without `--apply` the console prints the command that would land the patch. With it, plain `git
apply` runs from the top of the directory's repository with the directory's prefix, or in a
directory outside any repository. A refused patch stays saved, git's exit code is said, and the
directory is as it was.

### C5: A copy whose patch was not taken whole is kept and named _(enforced: mechanical)_ ^c5

- **Subject**: the failure paths of a boxed chat.
- **Violated when**: a box whose patch was not saved whole is stopped, a kept box is not named by
  its provider id, or a box that failed before the model worked is left running.

When reading or saving the chat patch fails, the box may hold the only copy of the work: it is kept,
not stopped, and named by the provider's id beside the error, until the provider's idle stop deletes
it. A box already dead is said to be unrecoverable, with the reason. A failed push or baseline stops
the box before the model works; a saved patch, an empty one and a refused apply stop it too.

### C6: A resumed chat replays its log, and nothing runs again _(enforced: mechanical)_ ^c6

- **Subject**: `lion chat -c` and `lion chat -r <id, prefix or path>`.
- **Violated when**: a resumed chat reruns a logged command, an old alias or value fails to resolve,
  the old log is written to, or an unsettled command goes unnamed.

`-c` continues the last log by id; `-r` takes an id, a unique prefix or a path. The log's rows come
back as entries with their sequences, results, inbound marks and directives; usage rows are skipped.
The loop admits each logged turn's values in order, and the fold lands where it did
([[ADR-0007-the-record#^c4|ADR-0007/C4]]).

A last line cut mid-write is dropped and named; a bad line with lines after it refuses the resume.
The unsettled commands are named to the model in one notice from the console, as not run again. The
new session writes a new chat log, the whole record again.

### C7: The chat is a console over the loop _(enforced: process)_ ^c7

A chat adds three things to a run: a `wait` that asks the person, an `accept` that refuses `OUT{}`,
and a `history` read from a log. The turn table, the bounds, the record, the fold, the notes and the
hooks are the loop's, unchanged. The console composes tools the hub already has; it owns no tool of
its own.

What the box isolates and may reach, its content-addressed trees, declared write paths and receipts
belong to the sandboxed executor ([[ADR-0011-the-box|ADR-0011]], the box). The gate is code review
of anything that gives the chat its own scheduling, or its own path into a box.

## Decisions

### D1: `hub/chat.py`: `chat`, `entry_row`, `load_log` ^d1

Serves C1 and C6. `chat` runs the actor once with `wait`, `history`, `accept` and the round budget,
and hands each entry to `say` from the bus's `run.started`, `round.started` and `command.` events,
from `wait`, and once more at the end. `entry_row` writes a result as its five fields and a
directive as data; `load_log` reads rows back through `entry_of`, the reader the checkpoint in
`lionagi/checkpoint.py` also uses.

- **Landing evidence**: `tests/test_chat.py`: a plain turn and the next line on one record, each
  entry said as it lands, `OUT{}` refused, the pause, the log read back as history, the directives
  folding the same, the cut last line.

### D2: `console()` and the flags on `lion chat` ^d2

Serves C2 and C3. `console` takes the sandbox, the coding flag and a tree, and registers the tools
over the `Tree`: `LocalTree` of the directory, or a `BoxTree` of the box. `chat_main` builds a
`Sandbox` VM for `--shell` or `--code`, or for a boxed chat a `DaytonaSandbox` whose workdir is the
directory's own path, with `--image` defaulting to `python:3.12`. `--model` picks the backend
([[ADR-0009-backends|ADR-0009]]).

- **Landing evidence**: `apps/cli/tests/test_chat_command.py` (khive only in an actor's directory,
  the root rules, the hooks);
  `test_the_console_reads_and_codes_over_a_given_tree_with_the_box_guidance`;
  `test_the_local_tree_refuses_paths_that_leave_it`.

### D3: `prepare_box` and `take_patch`, the patch before the stop ^d3

Serves C3, C4 and C5. `prepare_box` starts the box, pushes, and commits the baseline, returning its
hash. At the end `chat_main` takes the patch under `asyncio.shield`, then stops the box; a failure
taking it keeps the box and names it. The default is print and save; `--apply` lands.

- **Landing evidence**: `tests/test_daytona.py`: the chat in a box end to end, the subdirectory
  apply, committed edits, the ignored tracked file, the kept box named; `tests/test_chat.py`: failed
  preparation and push stop the box.

### D4: `resolve_chat`, `fresh_log` and `unsettled` for the resume ^d4

Serves C6. `fresh_log` creates the chat log exclusively, with `-02`, `-03` when a session already
took that second's id; a start that fails before logging removes its empty log so `-c` keeps the old
one; `--log` refuses an existing path. `unsettled` parses each logged turn and names the aliases
with no RESULT.

- **Landing evidence**: `tests/test_chat.py` (the exact id beside its sibling, the failed start);
  `apps/cli/tests/test_chat_command.py` (the whole record logged again, the unsettled commands, the
  missing log).

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Apply the chat patch by default | print and save loses nothing, and the person reads the patch before it lands |
| `git apply --3way` under `--apply` | a conflict writes markers into the person's file; plain apply lands whole or not at all |
| Mounting the directory in the remote box | the box would write the person's tree directly, which is what the copy exists to prevent |
| Keeping every box after its chat for `-c` | the saved patch holds the work; a box is kept only when its patch could not be taken (C5) |
| A summary of the session as what resumes | the record is the memory; a summary is the model's opinion of it and folds nothing |
| Rerunning unsettled commands on resume | one may have had its effect before the session ended; the model is told and asks again |

## Consequences

- **S1**: The boundary: the sandboxed executor, its content-addressed trees, the write paths a box
  declares, its receipts and what it may reach are the kernel's ([[ADR-0011-the-box|ADR-0011]], the
  box). The chat reaches them only through the tools the console registers.
- **S2**: What resumes is the record. The notes outlive every chat anyway, one file per profile
  under `.lion/notes/`. The VM is new per chat, the round budget and the spend count start again,
  and a boxed chat starts from a fresh push of the directory as it stands: an earlier chat patch
  left unapplied is not in the new copy.
- **S3**: A run ended mid-turn does not resume ([[ADR-0007-the-record#^c4|ADR-0007/C4]]). Beside the
  chat, `lionagi/checkpoint.py` and `lion context` take and restore checkpoints of named values and
  a folded view ([[ADR-0019-the-checkpoint|ADR-0019]]). A new process of the long-running agent
  ([[ADR-0013-the-agent|ADR-0013]]) replays its last checkpointed record as history, and refuses one
  that no longer folds to the checkpoint's view.
- **S4**: The chat log holds every value whole; the console prints a result as one line. The closing
  line sums the cost on every call's envelope, failed attempts included; a call whose envelope
  carries no cost adds nothing and is not flagged.
- **S5**: `--offline` reaches the local VM only; nothing in the chat restricts a remote box's
  network.
- **S6**: The copy's baseline needs git in the image; an image without it fails the preparation, and
  the box stops before the model works. The remote box needs the `daytona` extra; without it the
  start fails on import before any box exists.
- **S7**: The code guidance names the branch of the person's tree and calls the box a VM; in a boxed
  chat the copy's repository starts from the baseline commit alone.
