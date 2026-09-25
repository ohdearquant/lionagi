---
adr: ADR-0011
status: draft
liveness: operating (three boxes under tests and live runs, the coding tools under tests and one live console run, the watch under tests and on the bench; the real-box tests are opt-in)
date: "2026-09-25"
area: box
kind: new
depends_on:
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0003-the-bounds|ADR-0003]]"
  - "[[ADR-0005-command-handling|ADR-0005]]"
  - "[[ADR-0006-the-notification|ADR-0006]]"
  - "[[ADR-0007-the-record|ADR-0007]]"
tags:
  - adr
  - runtime
  - tools
---

# ADR-0011: The box, the coding tools and the watch

## Context

A model that reads well next needs to run what it reads, and a model that edits needs the system to
watch the tree rather than believe its claims. Three places run a model's commands: a VM over the
console's directory, a container from an evaluation image, and a remote sandbox from any image. This
record fixes one shape for the three, what a command may touch, the three tools a fix is found and
written with, and the watch that ends a run on the tree's evidence.

The commands are handlers ([[ADR-0005-command-handling|ADR-0005]]); the watch is a section of the
notification ([[ADR-0006-the-notification#^c3|ADR-0006/C3]]) and its gate is the job's `accept`
([[ADR-0003-the-bounds#^c4|ADR-0003/C4]]); a whole value stays whole and the view budget is the only
size control ([[ADR-0007-the-record#^c2|ADR-0007/C2]]). Source: `hub/tools/box.py`, `shell.py`,
`docker.py`, `daytona.py`, `code.py`, `fs.py`, `watch.py`; the box half of
`apps/cli/lion_cli/chat.py`; `bench/swe/sandbox.py`.

## Definitions

- **box**: somewhere the model's commands run and files live, apart from this machine: a VM, a
  container or a remote sandbox, all one shape.
- **prelude**: a shell fragment every command in a box runs after, once: the environment to
  activate.
- **tree**: where the coding tools read, search and write: a directory on this machine, or the box's
  working directory.
- **coding tools**: `read_lines`, `search` and `edit`; `bash` and `python` run what they wrote.
- **watch**: the section that reports the change in a box turn by turn, and the gate on `OUT{}` that
  reads the same change.
- **source change**: the files of the box's diff that are neither tests nor new files outside a
  tracked top-level directory.
- **acceptance checks**: shell commands the model declares that fail now and must all exit 0 once
  the issue is fixed.
- **patch**: the box copy's diff since the baseline committed when the directory was pushed in.

## Assumptions

| #  | statement                                                                        | source                                                                              | if false                                       |
| -- | -------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | ---------------------------------------------- |
| A1 | Every box has bash, coreutils `timeout`, git and `cat`.                          | the evaluation images, `python:3.12-slim`, the console's own image                  | `exec` needs a per-box guard and a file API    |
| A2 | An `old` that occurs once is the right unit of an edit.                          | 952 bench turns; ambiguous edits refused ([[ADR-0012-the-bench|ADR-0012]], the bench)    | line-number edits, which drift after one edit  |
| A3 | A command run after a change is the model's verification, whatever it ran.       | on the bench the run that ran nothing after its edits was wrong; resolved runs ran tests | the gate names the tests it wants         |
| A4 | The threat a sandbox answers is the host, not the network.                       | a VM with no egress cannot install anything; a console over a project needs to      | the network is off by default                  |
| A5 | A new file outside a tracked top-level directory is scaffolding, never the fix. | django-12273 in the 952-turn Mini-50 bench run ([[ADR-0012-the-bench#^c9\|ADR-0012/C9]]): a root `repro.py` and a new `tt/` package counted as the fix, and the settle rule stopped the run unresolved after 15 turns | a fix that adds such a file starts no clock, and the gate refuses a finish that holds nothing else |

## Claims

### C1: One shape, whatever runs the command _(enforced: mechanical)_ ^c1

- **Subject**: every box the console or the bench hands a model.
- **Violated when**: a tool branches on the box's class, a box answers `exec` without rc, stdout,
  stderr and elapsed apart, or `run` reads anything but 124 at its timeout.

`Box`: `workdir`, `prelude`, `dead`; `start`, `exec(argv, stdin, timeout, env)`, `run(cmd, timeout,
env)`, `read`, `write`, `diff`, `stop`. `exec` is one argv after the prelude, once, with the streams
apart, ended in the box at its timeout: rc 137 with `timed_out` set by the clock; no timeout is no
deadline. `run` is a shell line from `workdir` under `bash -o pipefail`, its combined output, rc 124
at the timeout.

`BoxBase` derives `run`, `read` and `write` from `exec` and `diff` from git; a box with a file API
of its own overrides the two file verbs. `read` refuses a file that is not UTF-8, since an edit
through a lossy decode would rewrite every bad byte.

### C2: A command touches the box and nothing else of the host, and ends at its timeout whole _(enforced: mechanical)_ ^c2

- **Subject**: every `bash` and `python` call.
- **Violated when**: a command reads or writes a host path outside the mounted directory, a hung
  command holds the turn, or stdout is cut before the model sees it.

`bash` runs one line under `bash -o pipefail -c` from the directory, so `pytest | tail` still fails
when pytest does; `python` runs source through the box's own environment. The VM mounts the
console's directory at its own path and a named volume at `/opt` for the environment and its cache,
and takes the host's time zone as `TZ`; the host's home is not there. Each command runs under the
guest's `timeout -s KILL`; the host waits fifteen seconds longer, then ends its own client.

The result is rc, stdout, stderr and elapsed, whole. The network is on by default; `--offline`
attaches an internal network with no DNS, no egress and no reach to the host.

### C3: Three tools over a tree, the tree may be anywhere, and an edit lands once _(enforced: mechanical)_ ^c3

- **Subject**: every `read_lines`, `search` and `edit` call.
- **Violated when**: a tool reads a path outside the tree, an edit lands more than once or where
  `old` was absent, or a later write carries an earlier read.

`read_lines(path, start, end)` returns numbered lines with the file's length, or says the file is
empty. `search(pattern, path, include)` returns `file:line:text` rows, every match, `(no matches)`
for none, and raises on a bad pattern; `rg` when the tree has it, else `grep -rE`; both read hidden
files and skip `.git`. `edit(path, old, new)` replaces exactly one verbatim occurrence, refuses any
other count with the count named, and keeps each line's own ending. Edits serialise on one lock.

A tree on this machine refuses a path that resolves outside it: `..`, an absolute path elsewhere, a
symlink out. A tree in a box binds every path to `workdir` before the box sees it. `read` and
`list_dir` go over the same tree.

### C4: The watch reports the change as the tree shows it; the model's own checks end the run _(enforced: mechanical)_ ^c4

- **Subject**: every turn of a run with the watch section, and every `criteria` declaration.
- **Violated when**: a settle or stable count reads anything but the diff, a test or scratch file
  starts the clock, a passing check set is stored, or a passing set fails to end the run.

Each turn the section reads the box's diff and sorts its files into tests, scratch and source; a
digest over the source change names it. The line says which source files the fix touches and since
which turn; tests and scratch start no clock. Past `notice_after` unchanged turns it asks for
`OUT{}` or a named gap; `settle_after` ends the run.

`criteria(cmds, timeout)` declares the acceptance checks: run once on the tree as it stands, refused
when all exit 0; a stored set reruns when the digest moves, and the run ends when all pass.
Declarations serialise on one lock, so a refused set cannot erase an accepted one.

### C5: A finish is accepted only after something ran against the change _(enforced: mechanical)_ ^c5

- **Subject**: every `OUT{}` of a run gated by the watch's `accept`.
- **Violated when**: a finish lands with no change, with tests or scratch alone, with the source
  changed in the same turn, with no `bash` or `python` result that ran in a later turn, or while a
  declared check fails.

`accept` reads the diff afresh and recomputes the digest over the sorted source paths the section
used. A digest the section has not seen is refused: a command in the same turn ran beside the edit
and saw the tree before it. Otherwise it needs one verifier RESULT, `bash` or `python`, that reached
its handler in a turn after the change; a refused or raising command ran nothing, a non-zero rc did
run. Each refusal says what to run next.

The gate reads that a command ran, never what it ran: a test the model edited itself and then ran
counts as verification (S5).

### C6: The box's diff is what comes back, and it lands on the person's tree only when asked _(enforced: mechanical)_ ^c6

- **Subject**: every diff read out of a box, and every chat over a remote box.
- **Violated when**: the diff carries stderr text or bytecode, a git error reads as no change, the
  patch reaches the directory without `--apply`, or a failing apply changes the directory.

`git_diff` reads the work tree against the base plus new files, never bytecode; stdout alone is the
patch and a wrong rc raises. A chat over a remote box pushes the files git sees to the same path and
commits a baseline there, so the patch reads against it.

At the end the patch is printed and saved beside the chat log; with `--apply` it is applied by `git
apply` from the repository root, whole or not at all, a failure said and the directory left. The
patch is read before the box is stopped; a box whose patch cannot be taken whole is kept until its
idle stop.

## Decisions

### D1: `hub/tools/box.py` and three boxes ^d1

Serves C1, C2 and C6. `Box`, `BoxBase`, `git_diff`, `inside` and `BoxTree` in `box.py`. The VM
(`shell.py`) is one container per conversation over the mounted directory. The docker box, named
`lion-` and eight hex characters, runs any image under `sleep infinity`. The remote box sends stderr
to a file behind a per-call marker and splits its one stream on it. It is replaced up to three times
when it never comes up; a box that dies later is named in `dead`, and the bench retries once on a
fresh box.

- **Landing evidence**: `tests/test_box.py` (the derived verbs, the timeout seam on both transports,
  no deadline reaching `exec` bare), `tests/test_shell.py` and `tests/test_daytona.py` (stubs; the
  real box under an opt-in flag, and a test that every real-box test is opt-in).

### D2: `hub/tools/code.py` and `fs.py` over a `Tree` ^d2

Serves C3. `code(actor, tree)` registers the three handlers and `files(actor, root)` the two
readers; the console passes a tree of its directory or of the box, the bench a tree of the box. The
tools do not know which. Both trees list a directory alike: every entry, hidden ones included,
sorted, `/` after a directory. An edit gives a new line the ending of the line before it, so a file
with mixed endings stays mixed.

- **Landing evidence**: `tests/test_code_tools.py` (nine: exactness, the single-file filename, the
  empty file, CRLF and mixed endings, hidden files, paths that leave the tree, the listing);
  `tests/test_daytona.py`, the box tree's listing.

### D3: `hub/tools/watch.py`: `Watch`, `Criteria`, `criteria`, `accept` ^d3

Serves C4 and C5. The bench installs `watch.section`, `criteria` under a flag, `accept` as the job's
gate and the watch's `stop` as the stop rule, asked after the section has looked
([[ADR-0002-the-run#^c3|ADR-0002/C3]]); no console path installs it (S11). The state the section
keeps (source, since, stable, checks, passed) is written into the bench's row. The default
classifier calls a path a test under a `tests` directory, as a `conftest.py`, or by a `test_*` or
`*_test.py` name; no caller passes another.

- **Landing evidence**: `tests/test_watch.py` (the classifier, the notice then the stop, checks
  refused and rerun, the finish refused until a command ran, the racing declarations) and the solve
  test that lands an edit beside a refused finish.

### D4: Network on by default, `--offline` for none ^d4

Serves C2, on A4. A VM with no egress cannot install anything, and a console over a project needs
to; the person picks `--offline` when the directory holds what the network must not see.

- **Landing evidence**: the internal network measured without DNS, egress or host reach, 2026-09-21.

## Alternatives

| approach                                            | rejected because                                                                              |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| One box class with a backend switch                 | three transports differ in exec, files and liveness; a switch hides that                       |
| A VM per command                                    | 1.4 s per call and no state between calls; an exec into one VM is 60 ms                        |
| A host sandbox profile                              | a deprecated, undocumented profile language, and the host's kernel and tools exposed           |
| `bash` with `sed` and heredocs only                 | a shell line per look, and heredoc edits fought the model's tool-call training                 |
| Line-number edits                                   | numbers drift after the first edit in a file; a verbatim `old` does not                        |
| Trusting `OUT{}` once the diff is non-empty         | on the bench: four turns, no test, wrong                                                       |
| Requiring the tests the issue names                 | the model does not always know them; a command run is what it can always do                    |
| A turn hook instead of a section                    | a hook's line cannot gate `OUT{}`; the gate needs the digest the section keeps                 |
| Applying the box's patch by default                 | the patch is the person's to land; a partial apply leaves a tree nobody chose                 |

## Consequences

- **S1**: The model's tests run on Linux; a host-only behaviour is not seen from the console.
- **S2**: The `/opt` volume persists across conversations, so a second chat's environment sync is
  fast and a broken one is cleared by removing the volume. A chat that dies without `stop` leaves a
  VM named after the chat; a leaked remote box stops after sixty idle minutes and is then deleted.
- **S3**: A bench run on these tools measures these tools: a change to `code` or `shell` moves the
  bench number ([[ADR-0012-the-bench|ADR-0012]], the bench).
- **S4**: `search` in an image without `rg` falls back to `grep -rE`, which reads no ignore files;
  the evaluation images are such.
- **S5**: A model that edits and finishes in one turn pays one more turn, and the refusal says why.
  The gate cannot tell a test the model edited from one it did not; naming the edited test in the
  refusal is the next candidate if that shape repeats.
- **S6**: The box itself reads and writes anywhere; only the model's tools are bound to the tree.
  The bench's grader writes its patch through the box. The remote box passes each call's stderr and
  any stdin, and the push's archive, through files under its own `/tmp`.
- **S7**: The evaluation image's prelude activates one environment for the model's `bash` and the
  grader's `run`, so both see the same tree state; a timed-out `run` reads 124 where `exec` reads
  137.
- **S8**: The boundary: an executor over content-addressed trees with declared write paths and
  receipts is the kernel's. A box here is a process the model's commands run in apart from this
  machine, its patch the only thing that comes back, and nothing here is a receipt. Two sandboxes
  with receipts neither can see is the fork the kernel's port closes, not this record.
- **S9**: The gate answers neither of two other miss shapes: a finish it accepts whose fix the
  grader passes only in part, and a run that reaches its time budget with no accepted finish. Both
  occur in the two unaided loop bench runs on these tools ([[ADR-0012-the-bench#^c5|ADR-0012/C5]]),
  where 15 and 18 of 29 misses ended at the time budget.
- **S10**: The VM's image, `lion-sandbox`, is built from `hub/tools/sandbox/Dockerfile` when a chat
  starts the VM without it; a failed build ends the chat with the build's tail. It is
  `python:3.12-slim` with git, ripgrep, jq, curl and `uv`, whose project environment is `/opt/venv`
  on the `/opt` volume, never the directory's own `.venv`.
- **S11**: No console path installs the watch. A chat over its own directory whose hooks carry the
  `tree-changed` round hook ([[ADR-0005-command-handling|ADR-0005]] S4) hears the files changed
  since the last turn and a moved HEAD; in a boxed chat that hook reads this machine's tree, which
  the model's tools do not change.
- **S12**: The derived `read` carries a file out of the box as base64. `git_diff` lists the new
  files NUL-separated, so a name git would quote reaches the diff as it is; no test pins such a
  name.
