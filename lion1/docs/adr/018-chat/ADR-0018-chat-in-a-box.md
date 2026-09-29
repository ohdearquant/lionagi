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
- **own tools**: with a `claude_code` model, the Claude Code CLI's tools `--tools` names, run by the
  CLI inside each round.
- **link**: a terminal hyperlink (OSC 8) the console draws: a URL to itself, a path naming an
  existing file to `vscode://file/<abs>[:line[:col]]`, which opens it in the editor.
- **config file**: `~/.lion/config.toml` and `<dir>/.lion/config.toml`, whose `[chat]` table holds
  defaults for the flags.

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
entries with sequences, results, inbound marks and directives; usage and `BOX` rows are skipped. The
loop admits each turn's values in order, and the fold lands where it did
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
  prompt leaves the loop without its SIGINT handler, or the status line shows a cached value.

On a terminal a paste of many lines is one message, Enter sends, Alt-Enter breaks a line, Up recalls
earlier messages from `.lion/history`, and Ctrl-C clears the line or, on an empty one, ends the
chat. A pipe keeps the plain line reader. The status line sits under the prompt and prints at each
round notice under `--verbose`. The flags' defaults come from the config file: the nearer file wins
per key, a flag on the line over both.

### C9: A first Ctrl-C cuts the call; a fold, not a failed directive, restarts a session _(enforced: mechanical)_ ^c9

- **Subject**: `Interrupt`, `Run.interrupt` and the `claude_code` session under `--tools`.
- **Violated when**: a Ctrl-C mid-call waits for the round, a cut ends the run, a failed directive
  restarts the session, or an own-tool call misses the record.

Mid-round a first Ctrl-C cuts the backend call in flight: the round ends with a notice and no turn,
the run goes on, and a `claude_code` session resumes where it was; with no call in flight the round
settles first. Only a result that changed the view carries its `spec`, so a failed directive leaves
the session as it was. `--tools`, for a `claude_code` model only, names the CLI's own tools; each
call passes the console's gate and lands on the record as a result the model can hide.

### C10: The console shows the model working, and folds what finished _(enforced: mechanical)_ ^c10

- **Subject**: `Console.working`, `Console.idle`, `_Working` and the status line's head on a
  terminal.
- **Violated when**: a call in flight shows nothing moving, a finished result stays open above the
  next, the status line names no model, or an own-tool call prints its arguments whole.

On a terminal the working indicator runs under the scrollback from a round's start until its turn
lands, `running` while the turn's commands run. With `--verbose` a result prints its head to the
scrollback and parks its detail under the indicator until the next lands; a call of the model's own
tools prints as one line, `✓ Bash command=…`, clipped. The status line leads with the model and the
actor. A pipe gets none of this. A command named like an own tool is refused with where that tool
lives.
### C11: What the console shows, the person can open _(enforced: mechanical)_ ^c11

- **Subject**: `linked`, `Console._args` and `Console.notice`.
- **Violated when**: a relative path resolves against the process's cwd, a path that does not exist
  links, a link is drawn inside fenced code, or a URL carries the sentence's punctuation.

In prose a URL is a link to itself and a path naming an existing file, resolved against the chat's
directory and never the cwd, is a link that opens it in VS Code at the line a `:line` names. Fenced
code is left as written, a bare word never links, and a path the model wrote in a code span links
whole. A call's `path` argument links to the file or directory it names; a URL in a notice links to
itself.

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
`--model` picks the backend ([[ADR-0009-backends|ADR-0009]]).

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
with no RESULT. `load_log` hands back the `BOX` row, whose box and image stand in for a flag not
given, else `vm` and `python:3.12`; `git apply --check -R` tests the continued session's patch.

- **Landing evidence**: `tests/test_chat.py` (the exact id beside its sibling, the failed start,
  `test_a_continued_boxed_chat_runs_in_its_box_again_and_is_told_its_unapplied_patch`);
  `apps/cli/tests/test_chat_command.py` (the whole record logged again, the unsettled commands, the
  missing log).

### D5: `Editor`, `status_line` and `lion_cli/config.py` ^d5

Serves C8. `Editor` wraps a `PromptSession` with its own key bindings and `handle_sigint` off;
`Console.ask` uses it when stdin and stdout are terminals, `stdin_ask` otherwise. `status_line`
reads the actor's inbox, the run's context figure and last reported prompt, the profile's notes and
`spent(calls)`. `chat_config` reads the two files and `main` parses twice, the second with the
files' values as defaults; `lion config` prints the effective values with their sources.

- **Landing evidence**: `tests/test_chat.py`: the editor's paste, Alt-Enter, Ctrl-C and Up, the
  SIGINT handler left in place, the status line's live values and where it prints;
  `apps/cli/tests/test_config.py`: precedence and refusals.

### D6: `Run.interrupt`, `run.cut`, and a result's `spec` ^d6

Serves C9. `Run.interrupt` sets `pause` and cancels the call the loop holds in `run.call`; the loop
reads `run.cut` to tell that cancellation from the run's own and continues. `_message` renders a
failed result with `spec=None`, which `ClaudeCodeSession` reads before restarting. `backend_for`
takes the tool names and `chat_main` hands the session the console's actor as its gate.

- **Landing evidence**: `tests/test_chat.py`: the cut with its two controls;
  `apps/cli/tests/test_chat_command.py`: the Ctrl-C presses; `tests/test_claude_code.py`:
  `test_a_failed_context_directive_does_not_restart_the_session`.

### D7: `Console.working`, `Console.idle`, `signer` and `Actor.elsewhere` ^d7

Serves C10. `Console.working(what)` starts a transient rich `Live` at eight frames a second holding
a `_Working` (frame, what, elapsed) and any parked detail; `idle()` stops it. `chat_main` subscribes
`round.started` to `working("thinking")`, and `Console.__call__` turns it to `running` when a turn
carries commands. `signer(directory)` reads the actor id for the status line. `Actor.elsewhere` maps
a name to the sentence appended to the unknown-command error; the chat fills it with the own tools.

- **Landing evidence**: `tests/test_chat.py`:
  `test_the_working_indicator_lives_on_a_terminal_only_and_holds_the_last_detail_under_it`,
  `test_the_status_line_leads_with_the_model_and_the_khive_actor_when_asked`,
  `test_an_own_tool_call_folds_to_one_line_and_an_unknown_name_is_told_where_it_lives`.
### D8: `linked`, `_target` and the links in calls and notices ^d8

Serves C11. `linked(text, directory)` walks the prose outside fences token by token (an escape, a
link the model wrote, an autolink, a code span, a URL, a path) and rewrites URLs as autolinks and
existing paths as links to `vscode://file/…`, since rich's markdown drops `file:` links. `_target`
resolves a path with `os.path` and answers None for one that does not exist. `Console._args` links a
call's `path`, `Console.notice` a URL, both as rich markup.

- **Landing evidence**: `apps/cli/tests/test_links.py`: a URL without its sentence, the common
  spellings of a path, the resolution against the chat's directory and never the cwd, a fence, a
  code span, a call's directory argument, a notice's hyperlink.

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
| A progress bar, or a line per second, while a call runs | nothing is known of a call's length; a spinner with the elapsed time says only that it is alive, and leaves the scrollback clean |
| `file:` links for paths | rich's markdown drops them, and a browser shows a file where an editor at the line is what the person opens it for |

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
  does not come off the directory, or cannot be checked without git, the resume notice says so to
  the model and the person; only that session's patch is checked.
  `test_a_continued_boxed_chat_runs_in_its_box_again_and_is_told_its_unapplied_patch` resumes a
  boxed chat.
- **S11**: A cut round leaves no turn on the record, only a notice; a `claude_code` session killed
  mid-call resumes on the next call from the CLI's own persisted session. A hide of what the CLI's
  own tools did folds the record; the CLI's session keeps its own transcript until a directive or a
  fold restarts it from the folded view.
- **S12**: The config file cannot switch a `true` off for one run: the flags are switches, so a
  nearer file's `false` is the only override. `lion chat --help` shows the built-in defaults, not
  the files'.
- **S13**: The indicator and the parked detail are screen state: when the next result lands the
  parked detail leaves the screen, and the log holds it whole. A pipe sees the heads only.
- **S10**: Nothing in the chat installs `uv` in a box: the default `python:3.12` is used as
  published, while the VM's own image copies `uv` into `python:3.12-slim`. The box guidance tells
  the model to install what the image lacks.
