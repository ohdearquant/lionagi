---
adr: ADR-0018
status: draft
liveness: operating (the chat, its resume and the boxed chat, a resumed boxed chat included, are pinned by tests against scripted backends and a stubbed box; the one real-box test runs only when enabled; no test interrupts a boxed chat)
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
- **chat log**: `.lion/chats/<id>.jsonl` under the directory by default: a `BOX` row naming where
  the chat ran, then every record entry as one JSON line, written as it lands, with a usage row per
  backend call between them.
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
- **editor**: the line the person types on a terminal, an editor over the whole message
  (prompt_toolkit) rather than a read through the tty's line discipline.
- **status line**: `model · actor · inbox N · ctx ~F/B (last call P) · notes N (S chars) · $C`, read
  off the live run each time it is drawn; `actor` is the id `.khive/config.toml` under the directory
  signs as, or `no actor`.
- **working indicator**: `⠋ thinking · 12s` under the scrollback while a round's call is in flight,
  `running` once the turn's commands run; a finished result folds to its head, its detail parked
  under the indicator until the next result lands.
- **link**: a terminal hyperlink (OSC 8) the console draws: a URL to itself, a path naming an
  existing file to `vscode://file/<abs>[:line[:col]]`, which opens it in the editor.
- **response**: a reply's prose as the console showed it, constructs cut; `/copy N` counts back from
  the last one (1 = the last), a continued chat's replayed ones included.
- **diagram page**: `.lion/chats/<id>-<seq>-<n>.html`, a reply's `n`th mermaid block drawn by the
  browser, linked under the reply.
- **voice**: Alt-V at the editor: a recording from the microphone, transcribed on this machine
  (`mlx-whisper`, behind the `voice` extra) into the line at the cursor.
- **khive config**: the file whose `[actor].id` the chat's khive calls sign as: `--khive-config` or
  `[chat] khive_config`, else `<dir>/.khive/config.toml`; an explicit one is never a fallback.
- **config file**: `~/.lion/config.toml` and `<dir>/.lion/config.toml`, whose `[chat]` table holds
  defaults for the flags.
- **instruction files**: the person's instructions a chat composes into its guidance, those that
  exist and hold text, in order: `~/.lion/CLAUDE.md`, the directory's `AGENTS.md` and `CLAUDE.md`,
  then its `.lion/CLAUDE.md`.

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
flushed whole however the run ends. A first Ctrl-C cuts the call in flight and hands back; a second,
or one at an empty prompt, ends the chat.

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
  the old log is written to, or an unsettled command or an unapplied chat patch goes unnamed.

`-c` continues the last log; `-r` takes an id, a unique prefix or a path. The rows come back as
entries with sequences, results (C19), inbound marks and directives; usage and `BOX` rows are
skipped. The loop admits each turn's values in order, and the fold lands where it did
([[ADR-0007-the-record#^c4|ADR-0007/C4]]). A refused turn admits nothing; one an older reader ran
and today's refuses admits what that reader did.

A last line cut mid-write is dropped and named; a bad line with lines after it refuses the resume.
Unsettled commands are named to the model in one notice as not run again. The new session writes a
new log, the whole record.

### C7: The chat is a console over the loop _(enforced: process)_ ^c7

A chat adds three things to a run: a `wait` that asks the person, an `accept` that refuses `OUT{}`,
and a `history` read from a log. The turn table, the bounds, the record, the fold, the notes and the
hooks are the loop's, unchanged. The console composes tools the hub already has; it owns no tool of
its own.

What the box isolates and may reach, its content-addressed trees, declared write paths and receipts
belong to the sandboxed executor ([[ADR-0011-the-box|ADR-0011]], the box). The gate is code review
of anything that gives the chat its own scheduling, or its own path into a box. A change to the
coding guidance, `hub/guidance/code.md`, names the run it was tried on.

### C8: The person types into an editor, and the console tells the state _(enforced: mechanical)_ ^c8

- **Subject**: `Console.ask`, `Editor` and `status_line` on a terminal.
- **Violated when**: a pasted line arrives as its own message, a line over 1024 bytes is lost, a
  prompt leaves the loop without its SIGINT handler, or the status line shows a cached value. Also
  when an explicit khive config that cannot be read is answered by the directory's own.

On a terminal a paste of many lines is one message, Enter sends, Alt-Enter breaks a line, Up recalls
earlier messages from `.lion/history`, and Ctrl-C clears the line or, on an empty one, ends the
chat. A pipe keeps the plain line reader. The status line sits under the prompt and prints at each
round notice under `--verbose`. The flags' defaults come from the config file: the nearer file wins
per key, a flag on the line over both.

### C9: A first Ctrl-C cuts the call; a failed directive leaves a session as it was _(enforced: mechanical)_ ^c9

- **Subject**: `Interrupt`, `Run.interrupt` and the `claude_code` session.
- **Violated when**: a Ctrl-C mid-call waits for the round, a cut ends the run, a cancel arriving
  with a cut is lost, a failed directive restarts the session, or the session's CLI is handed a tool
  of its own.

Mid-round a first Ctrl-C cuts the backend call in flight: the round ends with a notice and no turn,
the run goes on, and a `claude_code` session resumes where it was; with no call in flight the round
settles first. Only a result that changed the view carries its `spec`, so a failed directive leaves
the session as it was.

A `claude_code` model gets no tools of its own: each call is one turn with none, so everything it
does is a command in its text that the console runs, gates and records.

### C10: The console shows the model working, and folds what finished _(enforced: mechanical)_ ^c10

- **Subject**: `Console.working`, `Console.idle`, `_Working` and the status line's head on a
  terminal.
- **Violated when**: a call in flight shows nothing moving, a finished result stays open above the
  next, or the status line names no model.

On a terminal the working indicator runs under the scrollback from a round's start until its turn
lands, `running` while the turn's commands run. With `--verbose` a result prints its head to the
scrollback and parks its detail under the indicator until the next lands. The status line leads with
the model and the actor. A pipe gets none of this.
### C11: What the console shows, the person can open _(enforced: mechanical)_ ^c11

- **Subject**: `linked`, `Console._args` and `Console.notice`.
- **Violated when**: a relative path resolves against the process's cwd, a path that does not exist
  links, a link is drawn inside fenced code, or a URL carries the sentence's punctuation.

In prose a URL is a link to itself and a path naming an existing file, resolved against the chat's
directory and never the cwd, is a link that opens it in VS Code at the line a `:line` names. Fenced
code is left as written, a bare word never links, and a path the model wrote in a code span links
whole. A call's `path` argument links to the file or directory it names; a URL in a notice links to
itself.
### C12: A response, or the whole chat, can be taken to the clipboard _(enforced: mechanical)_ ^c12

- **Subject**: `Console.command`, `Console.copy`, `Console.copy_all` and the editor's Alt-C.
- **Violated when**: `/copy` reaches the model, the text copied differs from the prose shown, a
  number out of range copies anything, `/copy all` drops or wraps a line, or a copy blocks the
  prompt.

`/copy` at the prompt puts the last response on the clipboard, `/copy N` the Nth back from it (2 =
the one before the last), and Alt-C the last without leaving the line being typed; the console says
what it copied, or why nothing. The route is the platform's tool (pbcopy, wl-copy, xclip), and OSC
52 through the terminal where there is none, as over ssh. A console command never reaches the model.

`/copy all` copies the whole chat, unwrapped: each message, response, call and result line in order,
a continued chat's earlier part first; the loop's notices stay in the log.

### C13: A mermaid block in a reply is drawn _(enforced: mechanical)_ ^c13

- **Subject**: `Console._turn` on a reply holding a ```` ```mermaid ```` fence.
- **Violated when**: a mermaid block prints as its source only, a page is written outside the chat
  log's directory, a fence inside a longer fence is taken as a block, or a chat without a log fails
  on a diagram.

Each closed mermaid block of a reply lands as a diagram page beside the chat log, named for the
chat, the entry and the block's place in it, and the console prints a link to the page under the
block's source. The page loads mermaid from a CDN and draws the block's text unchanged. A chat with
no log says a diagram was not saved and goes on.
### C14: A message can be spoken _(enforced: mechanical)_ ^c14

- **Subject**: `Voice` and the editor's Alt-V on a terminal.
- **Violated when**: a recording is sent without the person reading it, the transcription blocks the
  prompt, a prompt that ends leaves a recording running, or a missing extra ends the chat.

A first Alt-V records from the microphone and the toolbar says so; a second stops it and the words
land at the cursor, after a space when the line already holds one, for the person to read before
Enter sends them. The transcription runs off the loop; the prompt stays live. Enter, Ctrl-C or
Ctrl-D drops a recording still running. Without the extra, a microphone or a model, the toolbar says
why and the prompt goes on.

### C15: A failure is said on one line: a backend's ends the chat, a refused turn's shows nothing ran _(enforced: mechanical)_ ^c15

- **Subject**: the backends' own errors reaching `_chat_main`, and `Console._calls`.
- **Violated when**: a backend failure prints a traceback, skips the closing line or exits 0, the
  message sent is lost to `-c`, or a refused turn shows its calls as pending.

A backend's own failure (a model the CLI does not know, a timeout, a rate limit) ends the run. The
working indicator leaves, one red line carries the backend's message, the closing line still names
the calls, the cost and the log, and the exit code is 1. The record already holds the message that
was sent, so `lion chat -c` continues the chat from it.

The console reads a turn the way the loop does: a turn the loop refuses (its own frame written by
the model, a tag that will not parse) prints its prose, then one red line with the loop's reason
where its calls would be.

### C16: The person's instruction files close the guidance _(enforced: mechanical)_ ^c16

- **Subject**: `instructions`, `console` and `lion config`.
- **Violated when**: an instruction file is left out or composed out of order, a caller's own system
  gains them, or the chat opens without naming them.

Every chat composes the instruction files at the end of its model's guidance, each under its own
name (`~/.lion/CLAUDE.md says:`), a `claude_code` model too, whose CLI reads no setting sources. A
file reached twice counts once, and one that cannot be read stops the start by name. The chat's
opening lines and `lion config` name the files composed, with their size. `--code` adds no copy of
the root files of its own.

### C17: The guidance tells a turn that issues commands to state no result _(enforced: mechanical)_ ^c17

- **Subject**: the console guidance, `hub/guidance/console.md`.
- **Violated when**: the guidance leaves out that a turn issuing commands says what it is doing and
  never what their results are, or that results are read only in the notice the loop writes.

A turn's commands answer only after the turn ends, in the notice the loop writes. A model that
wanted a result within its turn wrote that notice itself, results invented, and built a reply on
them. The guidance says a turn that issues commands says what it is doing, never what their results
are: results are read only in the notice the loop writes. A turn that writes the notice is refused
([[ADR-0004-the-language#^c4|ADR-0004/C4]]).

### C18: The khive tool names every verb it takes _(enforced: mechanical)_ ^c18

- **Subject**: the `khive_ops` line of the guidance `console()` composes, and the chair's in `lion
  agent`.
- **Violated when**: a verb the bound client admits is missing from the line, or the line names a
  verb the client refuses.

The line lists the client's allow list by name, sorted, then the salience cap on `memory.remember`.
It gave a count and a summary before, and a model learned a verb was allowed only by calling it.

### C19: A model value in the chat log comes back as its JSON form _(enforced: mechanical)_ ^c19

- **Subject**: `jsonable`, the `default=` of every chat log entry writer and of the checkpoint's
  file.
- **Violated when**: a model value is logged as anything but its `model_dump(mode="json")`, a
  command typed as a model that accepts its own dump is handed anything but the model, or a value an
  older log holds as a string resumes as anything else.

A model value, a command's result or a program's value, is logged as `model_dump(mode="json")`,
whatever that is: an object of its fields, computed ones included, a list or a scalar. A `RootModel`
dumps its root, and a serializer may return any JSON. A resume reads that JSON back. A program reads
the fields of an object (`*b.title`), and a command typed as the model is handed the model,
validated from the JSON.

A model whose fields JSON cannot hold is logged as its string.

### C20: A model replays when its own validation accepts its own dump _(enforced: mechanical)_ ^c20

- **Subject**: a command typed as a model, in a chat resumed from a log that holds a value of it.
- **Violated when**: a command typed as a model is refused for a dump its validation accepts, runs
  on one it refuses, or is refused as anything but invalid arguments.

A model round-trips when its own validation accepts its own dump, which a `RootModel` and a plain
model do. One whose serializer and validation disagree, or whose dump leaves out a required field,
is that model's defect: the command typed as it is refused in the resumed chat (C6) as invalid
arguments, and the run goes on.

On that validation a computed field's key is ignored, refused if the model forbids extra fields,
kept as extra data if it allows them. A model with aliased fields replays under its field names,
which a command typed as it accepts only if the model validates by name.

## Decisions

### D1: `hub/chat.py`: `chat`, `entry_row`, `load_log` ^d1

Serves C1 and C6. `chat` runs the actor once with `wait`, `history`, `accept` and the round budget,
and hands each entry to `say` from the bus's `run.started`, `round.started` and `command.` events,
from `wait`, and once more at the end. `entry_row` writes a result as its five fields and a
directive as data; `load_log` reads rows back through `entry_of`, the reader the checkpoint in
`lionagi/checkpoint.py` also uses.

- **Landing evidence**: `tests/test_chat.py`: a plain turn and the next line on one record, each
  entry said as it lands, `OUT{}` refused, the pause, the log read back as history, the directives
  folding the same, the cut last line; `tests/test_actor_resume.py`:
  `test_a_two_out_turn_an_older_reader_ran_keeps_its_values_on_resume_and_a_refused_one_does_not`.

### D2: `console()` and the flags on `lion chat` ^d2

Serves C2 and C3. `console` takes the sandbox, the coding flag, a tree and `about_box`, and
registers the tools over the `Tree`: `LocalTree` of the directory, or a `BoxTree` of the box.
`--box` is `vm` or `daytona`. Under `vm`, `chat_main` builds a `Sandbox` VM for `--shell` or
`--code`; for a boxed chat, a `DaytonaSandbox` whose workdir is the directory's own path, `--image`
defaulting to `python:3.12`, and as `about_box` the box guidance naming its kind and image.
`--model` picks the backend ([[ADR-0009-backends|ADR-0009]]). `--code` asks git for the branch
within one `GIT_TIMEOUT`.

- **Landing evidence**: `apps/cli/tests/test_chat_command.py` (khive only in an actor's directory,
  the root rules, the hooks);
  `test_the_console_reads_and_codes_over_a_given_tree_with_the_box_guidance`;
  `test_the_local_tree_refuses_paths_that_leave_it`; `tests/test_chat.py`:
  `test_a_resumed_coding_chat_whose_git_hangs_starts_with_the_branch_said_unknown_within_its_bound`.

### D3: `prepare_box` and `take_patch`, the patch before the stop ^d3

Serves C3, C4 and C5. `prepare_box` starts the box, pushes (D17), and commits the baseline,
returning its hash. At the end `chat_main` takes the patch under `asyncio.shield`, then stops the
box; a failure taking it keeps the box and names it. The default is print and save; `--apply` lands.
`take_patch`'s host git calls have `GIT_TIMEOUT` (30 s) each, the box's diff calls 120 s each; an
apply stopped at it may have written part of the patch, so the directory is said to be unknown, and
the box stops.

- **Landing evidence**: `tests/test_daytona.py`: the chat in a box end to end, the subdirectory
  apply, committed edits, the ignored tracked file, the kept box named; `tests/test_chat.py`: failed
  preparation and push stop the box;
  `test_a_patch_apply_that_hangs_is_stopped_and_what_it_changed_is_said_to_be_unknown`.

### D4: `resolve_chat`, `fresh_log` and `unsettled` for the resume ^d4

Serves C6. `fresh_log` creates the chat log exclusively, with `-02`, `-03` when a session already
took that second's id; a start that fails before logging removes its empty log so `-c` keeps the old
one; `--log` refuses an existing path. `unsettled` parses each logged turn and names the aliases
with no RESULT. `load_log` hands back the `BOX` row, whose box and image stand in for a flag not
given, else `vm` and `python:3.12`; `git apply --check`, reversed then forward, tests the continued
session's patch within one `GIT_TIMEOUT`.

- **Landing evidence**: `tests/test_chat.py` (the exact id beside its sibling, the failed start,
  `test_a_continued_boxed_chat_runs_in_its_box_again_and_is_told_its_unapplied_patch`,
  `test_a_resumed_chat_tells_a_patch_git_could_not_check_as_unknown_with_gits_reason`,
  `test_a_resumed_chat_whose_git_hangs_is_told_its_patch_could_not_be_checked_within_one_bound`);
  `apps/cli/tests/test_chat_command.py` (the whole record logged again, the unsettled commands, the
  missing log).

### D5: `Editor`, `status_line` and `lion_cli/config.py` ^d5

Serves C8. `Editor` wraps a `PromptSession` with its own key bindings and `handle_sigint` off;
`Console.ask` uses it on a terminal, `stdin_ask` otherwise. `status_line` reads the actor's inbox,
the run's context figure and last reported prompt, the profile's notes and `spent(calls)`.
`chat_config` reads the two files and `main` parses twice, the second with their values as defaults;
`lion config` prints each value with its source. An explicit khive config rides every `kkernel exec`
as `--config`, over the cwd's (measured); missing, or naming no actor, it refuses the start by name.

- **Landing evidence**: `tests/test_chat.py`: the editor's paste, Alt-Enter, Ctrl-C and Up, the
  SIGINT handler left in place, the status line's live values and where it prints;
  `apps/cli/tests/test_config.py`: precedence and refusals.

### D6: `Run.interrupt`, `run.cut`, and a result's `spec` ^d6

Serves C9. `Run.interrupt` sets `pause` and cancels the call the loop holds in `run.call`; the loop
continues when `run.cut` is set and the task's count of cancel requests did not rise during the
call; otherwise the cancellation is the run's own. `ClaudeCodeSession` reads what changed off the
view, which a failed result leaves as it was. `chat_main` calls `backend_for` with the model alone,
so the session runs `--tools ""` and `--max-turns 1`. On `-c`, `chat_main` seeds the session's
`base` from the log's last bill.

- **Landing evidence**: `tests/test_chat.py`: the cut with its two controls, and a cancel landing
  with a cut in both orders;
  `test_a_continued_chat_seeds_its_sessions_first_figure_from_the_last_billed_call`;
  `apps/cli/tests/test_chat_command.py`: the Ctrl-C presses,
  `test_a_claude_code_model_gets_no_tools_of_its_own_one_turn_per_call_and_no_gate_hook`;
  `tests/test_claude_code.py`: `test_a_failed_context_directive_does_not_restart_the_session`.

### D7: `Console.working`, `Console.idle` and `signer` ^d7

Serves C10. `Console.working(what)` starts a transient rich `Live` at eight frames a second holding
a `_Working` (frame, what, elapsed) and any parked detail; `idle()` stops it. `chat_main` subscribes
`round.started` to `working("thinking")`, and `Console.__call__` turns it to `running` when a turn
carries commands. `signer(directory)` reads the actor id for the status line.

- **Landing evidence**: `tests/test_chat.py`:
  `test_the_working_indicator_lives_on_a_terminal_only_and_holds_the_last_detail_under_it`,
  `test_the_status_line_leads_with_the_model_and_the_khive_actor_when_asked`.
### D8: `linked`, `_target` and the links in calls and notices ^d8

Serves C11. `linked(text, directory)` walks the prose outside fences token by token (an escape, a
link the model wrote, an autolink, a code span, a URL, a path) and rewrites URLs as autolinks and
existing paths as links to `vscode://file/…`, since rich's markdown drops `file:` links. `_target`
resolves a path with `os.path` and answers None for one that does not exist. `Console._args` links a
call's `path`, `Console.notice` a URL, both as rich markup.

- **Landing evidence**: `apps/cli/tests/test_links.py`: a URL without its sentence, the common
  spellings of a path, the resolution against the chat's directory and never the cwd, a fence, a
  code span, a call's directory argument, a notice's hyperlink.
### D9: `to_clipboard`, `Console.command`, `Console.copy` and `_prose` ^d9

Serves C12. `_prose` cuts a turn's constructs by the parser's own spans, the one function the view
and the copy share, so what is copied is what was shown; `Console.__call__` appends each reply's
prose, a replayed one too, to `responses`. `Console.command` runs `/copy [N]` and the chat's `ask`
loop asks again after one. `to_clipboard` tries each tool present on stdin, uncaptured (xclip and
wl-copy fork a child that serves the selection), then writes the OSC 52 escape.

`_plain` adds each entry's plain lines to `transcript`, which `/copy all` joins.

- **Landing evidence**: `apps/cli/tests/test_copy.py`: the last and the Nth-back response, a number
  out of range, the prose without its constructs, the whole chat unwrapped, the route order and the
  OSC 52 fallback, the command never sent, Alt-C.

### D10: `mermaid_blocks`, `diagram_page` and `_fences` ^d10

Serves C13. `_fences` walks the reply line by line, split on newlines only (`splitlines` also breaks
on characters a code line may hold). A fence closes on a bare run of its own character at least as
long, so a fence inside a longer one is text. `Console._turn` splits the prose at each block's end,
writes `diagram_page(source)` (the source HTML-escaped inside `<pre class="mermaid">`) and prints
the page's `file:` URI as a link.

- **Landing evidence**: `tests/test_mermaid.py`: the blocks found and the nested fence, the page's
  escaping, the page written and linked, the chat without a log.
### D11: `Voice`, `_off_loop` and `_real` ^d11

Serves C14. `Voice.press` toggles: it starts `record()` or stops it and hands the audio to
`transcribe` on a daemon thread, whose outcome `_off_loop` settles on the loop with
`call_soon_threadsafe` (not the loop's executor: its shutdown would join a transcription still
running). `_real` builds the microphone through sounddevice at 16 kHz mono and Whisper through
mlx_whisper, imported on the thread since the import takes seconds; both are injectable for tests.
`Editor.ask` calls `Voice.reset` when the prompt ends.

- **Landing evidence**: `tests/test_voice.py`: the toggle, the text at the cursor, the space after a
  word, the failures said on the toolbar, one transcription at a time, the reset, the extra missing.

### D12: `Console.failed` and the backend errors caught in `_chat_main` ^d12

Serves C15. `_chat_main` catches `ClaudeCodeError`, `CodexError` and `OpenRouterError` around the
run, beside the interrupt: `Console.failed` stops the indicator and prints the message in red, the
run is closed as `failed`, and `SystemExit(1)` is raised after the closing line. `chat` had already
flushed the record, so the log holds the message; `chat_main` removes only an empty log.
`Console._calls` reads a turn through the loop's own `_read`, so the refusal it prints is the
`LNDLError` the loop raised.

- **Landing evidence**: `apps/cli/tests/test_chat_command.py`: the failed backend, exit 1 and the
  `-r` continuation; a faked frame printing no call arrow. Controls: without the clause the raw
  error; with `lndl.parse` for `_read` the arrow.

### D13: `instructions`, `_shown` and where the chat names them ^d13

Serves C16. `instructions(directory)` in `config.py` lists the files that exist and hold text.
`console` appends each after `guidance("console", …)` when no `system` is given, named by `_shown`
from the directory, else `~/`, else whole; `code.md` has no `$rules`. `_chat_main` says them after
the banner; `config_main` prints one comment line each, or where it looked.

- **Landing evidence**: `apps/cli/tests/test_chat_command.py`: the order, the names, a caller's
  system left whole, the opening line; `apps/cli/tests/test_config.py`: `lion config`'s lines, an
  empty file skipped. Control: without the append the composition test fails.

### D14: The sentence in `hub/guidance/console.md` ^d14

Serves C17. The sentence follows the one that sends a question needing a fact to a command first, in
the first paragraph of `console.md`, which `console()` composes into every chat that names no system
of its own.

- **Landing evidence**: `tests/test_guidance.py`:
  `test_each_guidance_file_is_read_by_name_and_its_fields_filled` holds the sentence. Control:
  without it the test fails.

### D15: The tool line writes the client's own `allowed` ^d15

Serves C18. `console` in `lion_cli/chat.py` and `chair` in `lion_cli/agent.py` join
`sorted(khive.allowed)` of the `Bounded` client they register, so the line and the boundary read one
set.

- **Landing evidence**: `apps/cli/tests/test_chat_command.py`:
  `test_a_console_in_an_actors_directory_gets_khive_signing_as_that_actor`; `tests/test_agent.py`:
  `test_a_chair_wakes_on_the_owners_mail_reads_its_directory_and_answers_on_the_thread`. Control:
  the old line fails both.

### D16: `jsonable` in `lionagi/checkpoint.py`, the writers' `default=` ^d16

Serves C19 and C20. `jsonable` returns `model_dump(mode="json")` for a pydantic model, whatever form
that takes, and the `str` of anything else, or of a model whose dump raises pydantic's serialization
error. `Console` writes each chat log entry row with it, the long-running agent its checkpointed
record, and the checkpoint its file and each value its restore shows; a usage row is written with
`str`. `entry_of` hands the JSON back as written, and a Spec field typed as the model validates it
into the model again.

- **Landing evidence**: `apps/cli/tests/test_chat_command.py`:
  `test_a_logged_model_value_resumes_as_its_fields_and_a_command_typed_as_the_model_gets_the_model`,
  `test_a_logged_aliased_model_replays_under_its_field_names_which_only_a_by_name_command_accepts`,
  `test_a_resumed_program_reads_a_field_of_a_logged_model_value`,
  `test_a_logged_root_model_resumes_as_its_root_and_a_command_typed_as_it_gets_the_model`,
  `test_a_logged_model_its_own_validation_refuses_is_refused_at_replay_as_invalid_arguments`,
  `test_a_log_written_before_model_values_were_logged_as_fields_resumes_the_value_as_its_string`;
  the model value tests of `tests/test_checkpoint.py` and `tests/test_context.py`.

### D17: The push's git calls on this machine, each within the hub's `GIT_TIMEOUT` ^d17

Serves C3. A push lists the directory with `git ls-files` and, when that fails, asks `git rev-parse`
whether it is a repository, in `pack` of `hub/tools/box.py`, shared by the Daytona and Runta pushes.
Each call has that module's `GIT_TIMEOUT` (30 s); the hub does not import the command's. Past it
git's process group is killed and git reaped, and `TimeoutError` says git did not answer within the
bound; a kill refused again after the reap raises `PermissionError`, as no member is one this
process may signal. Nothing is pushed.

- **Landing evidence**: `tests/test_daytona.py`:
  `test_a_push_whose_local_git_never_answers_is_stopped_at_the_bound_and_said_to_have_timed_out`,
  `ls-files` and `rev-parse` each stalled;
  `test_a_stalled_git_whose_group_kill_is_first_refused_with_eperm_is_still_stopped`;
  `test_a_stalled_git_left_exited_and_unreaped_is_reaped_before_its_kill_is_tried_again`;
  `test_a_stalled_git_whose_kill_is_refused_again_after_the_reap_raises_permission_error`. Control:
  either call unbounded fails its own case alone, the kill retried without the reap fails the
  unreaped case, and a second refusal swallowed fails the
last.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Apply the chat patch by default | print and save loses nothing, and the person reads the patch before it lands |
| `git apply --3way` under `--apply` | a conflict writes markers into the person's file; plain apply lands whole or not at all |
| Mounting the directory in the remote box | the box would write the person's tree directly, which is what the copy exists to prevent |
| Keeping every box after its chat for `-c` | the saved patch holds the work; a box is kept only when its patch could not be taken (C5) |
| A summary of the session as what resumes | the record is the memory; a summary is the model's opinion of it and folds nothing |
| Rerunning unsettled commands on resume | one may have had its effect before the session ended; the model is told and asks again |
| Reading the person's line through `input()` with bracketed paste | the tty's line discipline still splits a paste into lines on some terminals and macOS drops any line over 1024 bytes without a word |
| A first Ctrl-C that waits for the round to settle | a long tool turn made the interrupt look ignored; the call is the person's to cut |
| Environment variables for the chat's defaults | a file the person can read back with `lion config` says where each value came from |
| Falling back to the directory's khive config when the explicit one is missing | the chat would sign as someone the person did not name; a refusal by file name is what they can act on |
| A progress bar, or a line per second, while a call runs | nothing is known of a call's length; a spinner with the elapsed time says only that it is alive, and leaves the scrollback clean |
| `file:` links for paths | rich's markdown drops them, and a browser shows a file where an editor at the line is what the person opens it for |
| Ctrl-Y, or a mouse button, for the copy | Ctrl-Y is the editor's yank, and a terminal has no buttons; a key the line survives and a command the person can number |
| Drawing the diagram in the terminal | no terminal draws mermaid; a page in the browser does, at full size, and the log keeps the source |
| Whisper through a remote API | the recording leaves the machine; the local model answers in seconds on Apple silicon and needs no key |
| Letting a backend's error propagate as the report | a traceback over the indicator says less than the backend's own line and skips the closing line that names the log |
| Letting the claude CLI read CLAUDE.md itself (its setting sources) | only one backend would get the files, and with them every setting and hook the CLI finds; composing them into the guidance serves every model alike |
| Every CLAUDE.md from the directory up to the filesystem root | the directories above a repository hold other projects' rules; the home file is the one every chat means to share |
| Copying the whole chat from the terminal's scrollback | the terminal wraps and indents what it shows; the copy is built from the entries, unwrapped |
| The claude CLI's own tools inside each round (`--tools`) | a second channel beside the commands: a model that called a command natively was told it did not exist, then wrote the loop's result frame itself with invented results |

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
- **S8**: With `--code` the root's `AGENTS.md` and `CLAUDE.md` enter the guidance whole, and the
  guidance goes out with every turn's call, so a long rules file adds its length to every turn's
  input.
- **S9**: The chat log's first row, `BOX`, names where the chat ran, and a resume runs there unless
  its flags say otherwise; a log older than the row resumes under `vm`. An unapplied chat patch is
  told, not built on: the new copy is a fresh push (S2). When the continued session's saved patch
  goes on but not off, or git cannot tell, the resume notice says so to the model and the person,
  with git's reason; only that session's patch is checked.
  `test_a_continued_boxed_chat_runs_in_its_box_again_and_is_told_its_unapplied_patch` resumes a
  boxed chat.
- **S11**: A cut round leaves no turn on the record, only a notice. A `claude_code` session killed
  mid-call resumes on the next call from the CLI's own persisted session, which keeps its own
  transcript until a restart that pays, or the fold, starts it over from the folded view.
- **S12**: The config file cannot switch a `true` off for one run: the flags are switches, so a
  nearer file's `false` is the only override. `lion chat --help` shows the built-in defaults, not
  the files'.
- **S13**: The indicator and the parked detail are screen state: when the next result lands the
  parked detail leaves the screen, and the log holds it whole. A pipe sees the heads only.
- **S10**: Nothing in the chat installs `uv` in a box: the default `python:3.12` is used as
  published, while the VM's own image copies `uv` into `python:3.12-slim`. The box guidance tells
  the model to install what the image lacks.
