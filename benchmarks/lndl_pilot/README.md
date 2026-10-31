# Batched LNDL rounds: the ADR-0132 D4 pilot

One task set, two loops, same model, tools, bound and instructions. The two agent profiles in
`.lionagi/agents/` differ only in `api_loop` (`react` or `lndl`). The result is read against
ADR-0132 D4: `lndl` is recommended only if it uses fewer requests per accepted task in total and
accepts no fewer tasks.

## Files

- `tasks.yaml`: 24 read-only questions about lionagi's own source (6 lookup, 6 cross-file,
  7 enumeration, 5 trace), each with an acceptance check (`accept.all_of` / `none_of`, each item a
  case-insensitive substring, or a regular expression after `re:`), the source line it was written
  against, a reference answer the check accepts and a decoy answer it rejects. The questions were
  written against commit `6fa69b19`, and the runs read a checkout of that commit. The runs used
  earlier versions of this file, kept under `runs/` as they ran: r2, r3 and r4 ran
  `runs/tasks.r2-r4.yaml`, r5 ran `runs/tasks.r5.yaml`, which changes the `t06` and `t20` checks,
  and this file then changes the `t24` check (below).
- `.lionagi/agents/d4-react.md`, `d4-lndl.md`: the profile pair, `anthropic/claude-haiku-5-5`,
  read tools, `max_extensions: 10`, `timeout: 300`.
- `summarize.py`: writes a run's `summary.json` from its `results.jsonl` and its `reading.jsonl`;
  `--check` exits 1 when the committed file differs from what it computes. It refuses rows that
  miss or repeat a task and loop of the run's task file (the one its `header.json` names, checked
  against `tasks_sha256`), and a reading whose source line the task's `truth` does not cite. A
  reading's source line is a `.py` file's full path and a line or range (`path:line`,
  `path:first-last`). The truth cites it when it gives that text, or when it gives the path
  elsewhere and the line under the file's name alone, as `t24`'s truth gives
  `lionagi/mcp/config.py:16` and then `config.py:19`. A source line written with the file's name
  alone is refused, and so is one with the full path where the truth names the file only by its
  name.
- `run_pilot.py`: the runner. Each task runs once per loop; the two runs of a task are adjacent in
  time and their order alternates by task, four tasks in flight. Requests, tool calls and wall time
  come from the `[api] spend:` line `li agent` prints (ADR-0132 D3); the task's own check is the
  first pass on acceptance, and the reading below decides.
- `runs/r2/`, `runs/r3/`, `runs/r4/`, `runs/r5/`: the runner wrote `header.json` (hashes of the
  runner, task set and profiles, the read tree's commit, the `li` version), `results.jsonl` (one
  row per leg), `summary.json` (totals and the D4 rule) and `table.md` (the per-task table); the
  committed `summary.json` is the one `summarize.py` writes in its place (below). `results.jsonl`,
  `table.md` and the totals in `summary.json` count acceptance by the checks as run; the tables
  below add acceptance on reading. `summary.json` gives the D4 rule both ways: `d4_rule_by_checks`
  on the checks' acceptance and `d4_rule_by_substance` on the reading, which is the one ADR-0132 D4
  decides by (its Amendment 1). Five files were added after each run: `answers.jsonl` (each run's
  answer, the leg's stdout with surrounding whitespace removed, as the check read it; its length is
  that row's `answer_chars`), `reading.jsonl` (the reading of each answer: one row per leg with
  `id`, `loop`, `check`, the check's result as run, `reading`, `right` or `wrong`, and the source
  line the reading used at `6fa69b19` and at `208bcc08`), `spend.jsonl` (each leg's `[api] spend:`
  lines from its stderr; the last one gives the row's requests, tool calls and wall time),
  `rounds.jsonl` (for each `lndl` leg, the syntax each round used, read from its conversation: `F`
  an `lndl` fence, `L` an `<lact` tag, `I` an `<invoke` tag, `O` an `OUT{`, `-` none of them) and
  `environment.json` (the lionagi checkout the run's environment installed and the read tree's
  checkout, read after the run).
- `runs/run_pilot.as-run.py.txt`: the runner exactly as r2, r3 and r4 ran it, byte for byte
  (sha256 `911c41cd…`, the `runner_sha256` in their `header.json`).
- `runs/r5/run_pilot.as-run.py.txt`: the runner exactly as r5 ran it, byte for byte (sha256
  `46234f89…`, r5's `runner_sha256`). The two as-run copies parse to the same syntax tree.
  `run_pilot.py` differs from r5's copy in one comment, in its imports and in the functions named
  below, and from the r2-r4 copy also in its `noqa` comments and layout (below).
- `runs/tasks.r2-r4.yaml` and `runs/tasks.r5.yaml`: the task set exactly as those runs ran it,
  byte for byte (sha256 `3c29864d…` and `e969e5a3…`, the `tasks_sha256` in their `header.json`).

## Runs

| run | lionagi | lndl round with no block |
|---|---|---|
| r2 | `4d373f9c` | continues with no error |
| r3 | `aca2e2e9` | a repair round that says nothing ran (ADR-0132 D2) |
| r4 | `d8838f28` | as r3; the repair round also says how a block opens and closes, the reader takes indented, longer and CRLF fences, and an empty block is a repair round |
| r5 | `208bcc08` | as r4 |

Each run's environment had lionagi installed in editable mode from a checkout of the commit in the
table: `4d373f9c94f646fe894b111071609461c9198301`, `aca2e2e9ab3f8000af3db0dfd0f7c2206640859c`,
`d8838f28ed9b3e218dcf834ff2048c5fb016139e` and `208bcc08dbdc512f31c142abf69c256f79a6536a`. All four
report version 0.35.5, which is all `header.json` records (`runner_li`), so each run's
`environment.json` records the commit, read from its checkout after the run: the checkout's HEAD,
the time its reflog last moved HEAD (before the run's `started_utc` in each run) and its count of
changed or untracked files (0 in each). The read tree's checkout is recorded the same way.
`aca2e2e9` is not on `main`: it is a commit of the branch merged as `d8838f28`, two commits
past `4d373f9c`, and the only file in the Python package it changes from `4d373f9c` is
`lionagi/operations/lndl_middle/lndl_middle.py`.

| run | loop | accepted by the checks | accepted on reading | requests | requests per accepted | tool calls | wall time |
|---|---|---|---|---|---|---|---|
| r2 | react | 23 / 24 | 24 / 24 | 94 | 3.92 | 56 | 295.9 s |
| r2 | lndl | 18 / 24 | 18 / 24 | 119 | 6.61 | 34 | 330.4 s |
| r3 | react | 22 / 24 | 23 / 24 | 95 | 4.13 | 59 | 315.9 s |
| r3 | lndl | 23 / 24 | 23 / 24 | 79 | 3.43 | 37 | 210.5 s |
| r4 | react | 23 / 24 | 24 / 24 | 91 | 3.79 | 59 | 290.7 s |
| r4 | lndl | 22 / 24 | 23 / 24 | 80 | 3.48 | 47 | 236.4 s |
| r5 | react | 23 / 24 | 23 / 24 | 99 | 4.30 | 65 | 307.1 s |
| r5 | lndl | 23 / 24 | 22 / 24 | 79 | 3.59 | 43 | 267.9 s |

Requests per accepted task divides a run's requests by the tasks accepted on reading.
`summary.json` stores it to three places, as the runner does (`3.435` for 79 over 23); the tables
here give two places computed from the counts (3.43), not the stored value rounded again. Wall time
is the sum of the legs' own `wall_seconds`; each run had up to four legs going at once
(`--concurrency 4`), so it is not the time the run took.
`tests/benchmarks/test_lndl_pilot.py` recomputes these tables, the counts below that decide each
run, and the figures ADR-0132 gives for the runs, from each run's files.

Each task's check (`accept` in its task file) is a first pass on its answer. Every answer in r2
to r5, accepted by its check or not, was then read against the `truth` line its task records (at
`6fa69b19`, the tree the runs read), and ADR-0132 D4 takes the reading as the decision. Read that
way, r2 fails the rule, r3 meets it with no margin (23 accepted tasks on each side), and r4 fails
it (`lndl` 23 against ReAct 24). r5, the run ADR-0132 D4 names as deciding, fails it: `lndl` 22 at
3.59 requests per accepted task against ReAct 23 at 4.30. By its checks r5 meets the rule with 23
on each side; the difference is the `lndl` answer to `t24`.

The reading is one reader's judgment, made by the author of this change knowing which loop wrote
each answer; no blind second reading was done. It judges what each answer says to its question and did not check every side claim;
each task's `truth` line is the source line it was read against, so another reader can redo it. An
answer counts as right when it gives the value or name the `truth` line records, also when it
corrects a premise of the question on the way. Two tasks turn on that rule (source lines at
`6fa69b19`):

- `t03` asks for the default `max_tokens` the Anthropic messages endpoint gets. Six accepted
  answers say the endpoint's request model has no default of its own
  (`lionhub/src/lionhub/providers/anthropic/messages.py:86`) and give 8192 as the value lionagi's CLI applies
  (`lionagi/cli/agent.py:89`, `:1027-1030`): the r3 ReAct, r4 `lndl` and r5 ReAct answers open
  with the missing default, and the r2 ReAct, r3 `lndl` and r4 ReAct answers open with 8192. The
  reading counts all six as right, since 8192 is the value the task records. Counted wrong, the
  three that open with the missing default make r3 `lndl` 23 against ReAct 22 and r4 `lndl` 22
  against ReAct 24, neither outcome changing, and r5 22 on each side, which meets the rule; all six
  make r3 22 against 22 and r4 `lndl` 22 against ReAct 23, also unchanged. r5's outcome turns on
  this reading of its ReAct answer to `t03`, among others (below), which ends "So the answer is
  8192 for that CLI API-launch path."
- `t05` asks for "the default max_tokens value for the BashTool's timeout field in milliseconds";
  the field holds a timeout, not a token count. All eight answers give 30000 ms, the value the task
  records (`lionagi/tools/code/bash.py:35`). Six of them also say the question's `max_tokens` does
  not fit the field (the r2 ReAct, r2 `lndl`, r3 ReAct and r4 ReAct answers and both r5 answers), and
  the r3 `lndl` answer says "It is a time limit, not a token count." Counted wrong, those six leave
  r2 failing the rule and r3 meeting it (`lndl` 23 against ReAct 22), make r4 meet it (23 against
  23) and leave r5 failing it (`lndl` 21 against ReAct 22); counting the r3 `lndl` answer wrong too
  leaves r3 at 22 against 22, which still meets it.

r4 and r5 each fail the rule by one task. Counting any one of r4's accepted ReAct answers wrong,
such as its `t05` answer, would make r4 meet the rule; in r5, counting any one of its accepted ReAct
answers wrong, such as its `t03` answer, or either `lndl` answer read as wrong (`t03`, `t24`) right,
would do the same.

The checks are substring and regular-expression tests, so a wrong answer that repeats the expected
words or digits can pass one, and a right answer in other words can fail it; that is why the
reading decides. `t04`'s check accepts "20 extensions, not 10", `t11`'s accepts "7 tools (lines
286-294) ... does not include context", and `t12`'s accepts "5 (not 8) ... 15.0 (not 10.0) ... 60
(not 30)". The `t24` check as changed after r5 needs `.lionagi` and both assignments,
`RUNS_DIR = RUNS_ROOT` and `JOBS_DIR = LIONAGI_HOME / ...`: it rejects "RUNS_DIR is an alias of
RUNS_ROOT", and it would accept a denial that quotes both. Every answer in r2 to r5 was read, so each
answer a check misjudged is among the differences listed below.

The reading and the checks differ in the `reading.jsonl` rows whose `check` and `reading`
disagree: ten answers in r2, r3 and r4, and one in r5 (r5's `t06` and `t20` checks take the
answers the earlier ones rejected):

| task | runs | loop | check | reading | why |
|---|---|---|---|---|---|
| `t06` | r2, r3, r4 | both | rejected | right | The check expects `100000`. Each answer writes the cap as `100_000`, all but the r4 ReAct answer also as `100,000`, and names `_MAX_OUTPUT_BYTES` and `_SHELL_CONTROL` (`lionagi/tools/_subprocess.py:20`, `:18`). |
| `t20` | r4 | `lndl` | rejected | right | The question asks which module defines the class; the answer names it by its import path, `lionagi.tools.code.search`, and says it did not open the file. The check expects the file path; the class is defined at `lionagi/tools/code/search.py:193`. |
| `t24` | r2, r3, r4, r5 | `lndl` | accepted | wrong | Each answer gives `RUNS_ROOT` in place of `RUNS_DIR`: r3 says no `RUNS_DIR` exists, r2 that its grep shows none in `lionagi/mcp/config.py`, r5 that its grep shows none anywhere, and r4 that the module defines none; r4 and r5 add that they read the file only in part, and r4 that it does not rule one out. Line 19 of that file is `RUNS_DIR = RUNS_ROOT`. The check these runs used asks for `.lionagi`, the default the question asks for, and the names `RUNS_DIR` and `JOBS_DIR`, which the question itself supplies, so it accepts these answers. |

The other rejected answers are not right on reading either (source lines here are at
`6fa69b19`): the five r2 `lndl` legs that spent all 11 requests (`t03`, `t04`, `t17`, `t18`, `t19`;
ADR-0132 D4, Result) give no value, each saying it could not confirm, list or determine what was
asked, and the r3 ReAct `t04` leg made no tool call and answered that none of its tool calls
returned results. r5's two are wrong:
the `lndl` answer to `t03` gives `32768` from another package in the read tree, where lionagi's
CLI default is `8192` (`lionagi/cli/agent.py:89`), and the ReAct leg on `t10` called a tool named
`grep`, which is not one of its read tools (`reader`, `search`, `context`), and then answered that
it could not find the constants. Three accepted answers carry an error in a side claim that does
not touch what the question asks: the r4 `lndl` answer to `t22` says the question gives
`_wire_secure_guards` the signature `(spec: AgentSpec, hooks: dict)`, which the question does not
(the rest of the answer matches `lionagi/agent/spec.py:48-56`); the r2 ReAct answer to `t05` says
`BashTool.call` substitutes the default, where the method is `handle_request`
(`lionagi/tools/code/bash.py:111`); and the r4 `lndl` answer to `t03` says the CLI applies the
fallback with `.get`, where it calls `setdefault` (`lionagi/cli/agent.py:1028`). The r2 ReAct answer to `t21` says `_wire_secure_guards` is
"imported from lionagi/agent/spec.py line 44"; that line is where the function is defined, and the
import is at `lionagi/cli/agent.py:266`. The reading does not count it as a fourth, since the module
and line it names are the definition the task records and only the verb is loose.

### r5, task by task

Each row gives the source line its reading used, at `6fa69b19`, the tree the legs read, and at
`208bcc08`, the code r5 ran, and per loop what the check did and what the reading found;
`tasks.yaml` records the rest of each task's truth. Of the 42 source-line citations in the tasks'
`truth` lines (158 lines in all), 12 citations in 9 tasks (28 lines) hold different text at the same
line numbers at `208bcc08`. Each citation's lines, taken as one block, are present unchanged at
`208bcc08`, and at one place only: the line numbers moved, the cited code did not. Those counts take
`t21`'s "line 272" as a citation and not its "line 271", which is inside the `265-271` it cites, and
read `t18`'s five citations by file name, such as `review.py:516`, as the files beside the
`lionagi/engines/__init__.py` it cites. The full commits are
`6fa69b196f45f65fe63ba640b8acc248e8c7cb5e` (read tree) and
`208bcc08dbdc512f31c142abf69c256f79a6536a` (the installed code r5 ran). On 2026-10-24, all 42
source blocks and their unique unchanged targets were compared against the actual code at those
two commits. The range mappings were transcribed into the fixture in
[`test_lndl_pilot.py`](../../tests/benchmarks/test_lndl_pilot.py). Those tests check every task
truth citation, recorded reading target and table target for consistency with the recorded fixture
and derive the counts above, without re-reading either historical commit. The fixture checks need
no historical Git objects or network.

| task | line at `6fa69b19` | line at `208bcc08` | `lndl` | ReAct |
|---|---|---|---|---|
| `t01` | `lionagi/_spec_limits.py:11` | `lionagi/_spec_limits.py:11` | accepted, right | accepted, right |
| `t02` | `lionagi/version.py:1` | `lionagi/version.py:1` | accepted, right | accepted, right |
| `t03` | `lionagi/cli/agent.py:89` | `lionagi/cli/agent.py:109` | rejected, wrong | accepted, right |
| `t04` | `lionagi/cli/agent.py:87` | `lionagi/cli/agent.py:93` | accepted, right | accepted, right |
| `t05` | `lionagi/tools/code/bash.py:35` | `lionagi/tools/code/bash.py:35` | accepted, right | accepted, right |
| `t06` | `lionagi/tools/_subprocess.py:20` | `lionagi/tools/_subprocess.py:21` | accepted, right | accepted, right |
| `t07` | `lionagi/tools/file/reader.py:24` | `lionagi/tools/file/reader.py:25` | accepted, right | accepted, right |
| `t08` | `lionagi/cli/agent.py:83` | `lionagi/cli/agent.py:89` | accepted, right | accepted, right |
| `t09` | `lionagi/agent/permissions.py:83-88` | `lionagi/agent/permissions.py:83-88` | accepted, right | accepted, right |
| `t10` | `lionagi/hooks/bus.py:71` | `lionagi/hooks/bus.py:71` | accepted, right | rejected, wrong |
| `t11` | `lionagi/tools/coding.py:286-294` | `lionagi/tools/coding.py:287-295` | accepted, right | accepted, right |
| `t12` | `lionagi/dispatch/outbox.py:48` | `lionagi/dispatch/outbox.py:48` | accepted, right | accepted, right |
| `t13` | `lionagi/protocols/messages/base.py:21-28` | `lionagi/protocols/messages/base.py:21-28` | accepted, right | accepted, right |
| `t14` | `lionagi/protocols/generic/event.py:30-39` | `lionagi/protocols/generic/event.py:30-39` | accepted, right | accepted, right |
| `t15` | `lionagi/session/control.py:13-17` | `lionagi/session/control.py:13-17` | accepted, right | accepted, right |
| `t16` | `lionagi/service/providers.py:34-43` | `lionagi/service/providers.py:34-43` | accepted, right | accepted, right |
| `t17` | `lionagi/work/form.py:40` | `lionagi/work/form.py:40` | accepted, right | accepted, right |
| `t18` | `lionagi/engines/__init__.py:10-59` | `lionagi/engines/__init__.py:10-59` | accepted, right | accepted, right |
| `t19` | `lionagi/libs/path_safety.py:11-13` | `lionagi/libs/path_safety.py:11-13` | accepted, right | accepted, right |
| `t20` | `lionagi/agent/factory.py:512-519` | `lionagi/agent/factory.py:512-519` | accepted, right | accepted, right |
| `t21` | `lionagi/cli/agent.py:265-271` | `lionagi/cli/agent.py:305-311` | accepted, right | accepted, right |
| `t22` | `lionagi/agent/spec.py:48-56` | `lionagi/agent/spec.py:48-56` | accepted, right | accepted, right |
| `t23` | `lionagi/mcp/config.py:35` | `lionagi/mcp/config.py:35` | accepted, right | accepted, right |
| `t24` | `lionagi/mcp/config.py:19` | `lionagi/mcp/config.py:19` | **accepted, wrong** | accepted, right |

`t20`'s `lndl` answer names the module by its import path and says it did not open the file; the
question asks for the module, so it is right. r5's outcome is one task wide: any one of its accepted
ReAct answers counted wrong, or either `lndl` answer read as wrong (`t03`, `t24`) counted right,
would make `lndl` meet the rule. `t03`'s ReAct answer is one of those: it says the request model
has no default and answers 8192 for the CLI's API launch path, read as right (see above).

After r5, `tasks.yaml`'s `t24` check was changed to require the assignments themselves
(`RUNS_DIR = RUNS_ROOT` and `JOBS_DIR = LIONAGI_HOME / ...`, as regular expressions) instead of the
two names the question supplies. Run against the committed answers, it rejects all four `lndl`
answers (r2 to r5) and accepts all four ReAct ones; no run has used it yet.

Each task in the two as-run task files also has a `none_of: []` beside `accept`. The runner reads
it only to refuse a task that has a non-empty one (below), since `check_answer` reads `none_of`
from `accept` alone; it is kept so that each file still matches the hash in its runs'
`header.json`. `tasks.yaml` has none.
The two as-run task files also keep two `enumerate_known_member` lines that are one line off at
`6fa69b19`, and no check reads them: `t13`'s `system` is at `lionagi/protocols/messages/base.py:24`,
and `t19`'s `.env` at `lionagi/libs/path_safety.py:12`. `tasks.yaml`, which no run has used, has
them corrected.

`run_pilot.py` takes each leg's requests from its spend line and counts a leg with none as zero
requests. `summary.json` records how many legs that was (`missing_spend`). A leg counted as zero
lowers its loop's requests, so the runner computes the D4 rule only while that number is 0 for both
loops; otherwise it writes `d4_rule` as `null` and says why in `d4_rule_unset`, and `summarize.py`
then leaves both D4 rules unset. It is 0 in r2 to r5, and `spend_lines` is at least 1 in all 192
rows. Each leg's spend lines are in its run's `spend.jsonl`.

## How the committed files differ from the runs' output

- `run_pilot.py` is `runs/run_pilot.as-run.py.txt` with `noqa` comments added, the `(r1)` at the end
  of one comment removed (both as-run copies keep it), the layout reformatted by ruff, and four
  changes. First, `summarize()` leaves the D4 rule unset when a leg had no spend line (above) or the
  rows are not the whole run, where the as-run copies computed it. Second, a run that stops early
  keeps what it has (`run_one()`, `run_pair()`, `main()`): a leg that raises in the runner, in its
  `subprocess.run` or after it (writing its output, or running its check), becomes a row with `rc`
  `harness-error` and no spend line; a leg that times out keeps its output so far, and bytes that do
  not decode are replaced in any leg's output; each row is appended to `results.jsonl` as its leg
  ends, and the summary and table count it only once it is written there (a row that cannot be
  written stops its task on a harness error); and an interrupt starts no further leg and still
  writes the summary and table (below). Third, `main()` makes the run directory only after it has
  checked that `ANTHROPIC_API_KEY` is set, that the read tree exists and `li` is an executable
  file, read the task file (a file that can be read, a YAML mapping with a `tasks` key, and no
  mapping in it naming a key twice), checked each task
  (`task_problems()`: an `id` of 1 to 100 letters, digits, `_` and `-` that no other task shares,
  letter case aside, since each leg's files are named after it; a non-empty `class` on one line
  and a non-empty `prompt`, both text that encodes as UTF-8; checks that are lists of non-empty
  strings with a non-empty `all_of`, each encoding as UTF-8; each regular expression compiling,
  not blank, and not matching an empty answer; and no non-empty `none_of` beside the `accept`
  block, which `check_answer` would ignore), checked `--concurrency`, `--loops` (each 1 to 100
  letters, digits, `_` and `-`, the name of a profile beside the runner, letter case included,
  and none named twice), `--only`, that no two legs would write the same files, and that `--out`
  is not empty and that no leg file the run will write would have a path too long for the file
  system; it then makes that directory with one `mkdir`, which refuses an existing path, a
  missing or dangling parent, a file or a link loop on the way, and a name or path the file
  system will not take, each with the OS's own reason, and started `git` and `li --version` for
  the header, so none of those checks failing, and neither program failing to start, leaves a
  run directory behind; a failed check, or a run directory that cannot be created, exits 2, and
  only the run directory itself is ever made. The `mkdir` follows links, so a path component
  replaced by a link to another existing directory after `--out` was given is followed, and a
  `..` after a link goes up from the link's target, as the OS resolves it; its message gives the
  OS's reason, not the component that failed. Each task in the two as-run task files has an
  empty `none_of` beside its `accept`. Fourth, `main()` names a task file that is in this
  directory by its path here in `header.json`, the form `summarize.py` reads (below); it writes
  each part of the path as its directory lists it, taking an entry whose name differs from the
  given one only in letter case when it is the same file, so a spelling that a case-insensitive
  volume accepts names it too and no link beside the file is taken for it. Of the files'
  top-level statements, `summarize()`, `run_one()`, `run_pair()` and `main()` are the ones whose
  `ast.dump` differs, and `run_pilot.py` adds two imports (`threading` and
  `collections.abc.Callable`), the `TASK_ID` pattern, `_path_max()`, the YAML loader that
  refuses a key named twice (`_TaskLoader`, `_mapping_without_repeats()` and the call that
  registers it) and `task_problems()`. With `missing_spend` at 0 and every leg run in all four
  runs, both give the same totals and rule: the test loads each as-run copy, checks its sha256
  against the run's `runner_sha256`, and recomputes the run's `summary.json` with it.
- In each `header.json`, `tasks_file` was the absolute path the runner as run wrote
  (`Path(args.tasks).resolve()`, line 141 of `runs/run_pilot.as-run.py.txt` and line 147 of r5's
  copy). It was replaced by `tasks.yaml` before the first commit and now names the copy of the
  task set that run used, `runs/tasks.r2-r4.yaml` or `runs/tasks.r5.yaml`,
  whose sha256 is the header's `tasks_sha256`. `tasks.yaml` beside the runner is the task set as
  it is now (sha256 `b938a95d…`), which no run used. `summarize.py` reads `tasks_file` only from
  inside this directory: an absolute path, or one that resolves outside it, is refused, whatever
  the file's hash. `run_pilot.py` now writes the path relative to this directory when the task
  file is in it, so a new run's header needs no edit.
- Each committed `summary.json` is the one `summarize.py` writes, which replaces the one the
  runner writes at the end of a run (see Running it): the totals and
  `d4_rule_by_checks` are the runner's own summary of `results.jsonl` (its `d4_rule`, renamed), and
  `d4_rule_by_substance` is the same summary with each leg accepted as `reading.jsonl` reads it. The
  two agree in r2, r3 and r4. In r5, `lndl_accepts_no_fewer` is true by the checks and false on
  reading (22 against 23). `summarize.py --check` passes on all four runs: for each run it checks
  that `reading.jsonl` has one row for each leg and that its `check` column is the run's
  `accepted`, and the tests check the counts in ADR-0132's Result table against it.
- Apart from those two edits and a final newline added to the JSON files, the files in `runs/`
  are as the runner wrote them: each run's `results.jsonl` and `table.md` are unchanged since their
  first commit. Each run's `answers.jsonl`, `reading.jsonl`, `spend.jsonl`, `rounds.jsonl` and
  `environment.json`, r5's copy of the runner and the two task files were added.
- The runner also writes each leg's prompt, stdout and stderr beside its results, and `li agent`
  keeps each leg's conversation under `home/`, the one directory every run shares (below). Of those,
  the stdout is committed as `answers.jsonl`, the stderr's spend lines as `spend.jsonl`, and the
  syntax of each round of each `lndl` conversation as `rounds.jsonl`, from which ADR-0132 counts the
  runs that opened with an `<invoke>` tag and the requests that went to rounds with no block. The
  prompts, the rest of the stderr and the conversations are not committed, and `.gitignore`
  beside the runner leaves them out of a `git add`, with `home/`, `tree/` and `venv/`. A run
  directory under one of those three (`--out home/r9`, say) passes the runner's checks and is
  ignored whole, its results included. A run whose results are to be committed goes outside
  those three; `runs/` is where this repo keeps them.

## Running it

The runner expects, beside it, `tree/` (the source checkout; r2-r5 read `6fa69b19`),
`venv/` (an environment with the lionagi to measure installed, so `venv/bin/li` exists), and
`home/` (an empty directory: the runner sets `HOME` to it and `LIONAGI_HOME` to `home/.lionagi`,
so the configuration in the real
home directory is not read) and `ANTHROPIC_API_KEY` in its environment. `.gitignore` beside the
runner keeps those three directories, and the prompt, stdout and stderr files the runner writes for
each leg, out of a commit. Each leg runs in `tree/`, and `li agent` looks for its profile in a
`.lionagi/agents/` in `tree/` and then in each directory above it, which is how it finds the
profiles beside the runner; a profile of the same name in a directory nearer to `tree/` would be
taken first. Then:

```bash
venv/bin/python run_pilot.py --tasks runs/tasks.r5.yaml --out runs/<name>
```

This repeats r5 with the task set it ran; `--tasks tasks.yaml` runs the current set, whose `t24`
check no run has used. A run is 48 `li agent` legs, two per task. `--only t01,t02` runs part of the
task set. A run that does not cover every leg of the whole task set, because of `--only`, an
interrupt, or a task that stopped on an error in the runner itself, still writes `summary.json` and
`table.md` from the rows it has: `summary.json` says why under `partial` and sets the D4 rule to
`null`, with `d4_rule_unset` giving the same reason (and the count of legs with no spend line, if
any), and `table.md` has a line for every task the run covers, marking a leg with no recorded row as
`not run` (it did not start, its row could not be written, or it ended after a third interrupt) and
a count missing from a leg's output as `-`. On an interrupt (Ctrl-C) no further leg starts and the
runner waits for the legs already running. A second interrupt stops that wait: the summary and table
are written from the rows so far, with `partial` saying that rows of legs still running may follow
in `results.jsonl`, and written again from all the rows once those legs end; a third interrupt keeps
them as first written. Either way the process exits once those legs end. The runner exits 2, with no
run directory, when a check before the run fails (above); 130 after an interrupt; and 1 when a task
stopped, or a leg raised, on an error in the runner itself.

A whole run's `summary.json` holds the runner's own summary (`d4_rule`, by the checks); once each
answer has been read and the reading written to `runs/<name>/reading.jsonl`,
`venv/bin/python summarize.py runs/<name>` writes the summary with both rules.

The summarizer recomputes only a whole comparison: the header's `task_count` must equal the task
file's whole set and its `loops` must contain both `react` and `lndl`, once each. It refuses a
recorded task subset with a `--only` cause, a loop subset or different loop set with a `--loops`
cause, and names both when both apply. For example, the runner can record `--loops react`, but the
summarizer cannot derive a two-loop comparison from it. Explicit `--only` selecting every task and
`--loops lndl,react` still describe a whole comparison. Missing or malformed header metadata has a
separate diagnostic; missing rows under a whole-run header remain an incomplete-run coverage
error. A refusal leaves the runner's existing `summary.json` unchanged.

The header also records the full `read_tree_head`. For each reading, the summarizer selects exactly
one `source_<hex-prefix>` key matching that head (8-40 lowercase hexadecimal characters) and checks
its citation against the task's truth. A missing or ambiguous matching key is refused, and a source
key for another tree cannot substitute for it. This selection follows the recorded tree, including
runs whose tree differs from the historical pilot's `6fa69b19`.
