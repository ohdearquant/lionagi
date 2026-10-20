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
  written against commit `6fa69b19`, and the runs read a checkout of that commit.
- `.lionagi/agents/d4-react.md`, `d4-lndl.md`: the profile pair, `anthropic/claude-haiku-5-5`,
  read tools, `max_extensions: 10`, `timeout: 300`.
- `run_pilot.py`: the runner. Each task runs once per loop; the two runs of a task are adjacent in
  time and their order alternates by task, four tasks in flight. Requests, tool calls and wall time
  come from the `[api] spend:` line `li agent` prints (ADR-0132 D3); the task's own check is the
  first pass on acceptance, and the reading below decides.
- `runs/r2/`, `runs/r3/`, `runs/r4/`: the runner wrote `header.json` (hashes of the runner, task
  set and profiles, the read tree's commit, the `li` version), `results.jsonl` (one row per run),
  `summary.json` (totals and the D4 rule) and `table.md` (the per-task table). These count
  acceptance by the checks as run; the tables below add acceptance on reading. Five files were
  added after each run: `answers.jsonl` (each run's answer, the leg's stdout with surrounding
  whitespace removed, as the check read it; its length is that row's `answer_chars`),
  `reading.jsonl` (each answer's reading: `check` as the run recorded it, `reading` `right` or
  `wrong`, and `source_6fa69b19`, the source line it was read against), `spend.jsonl` (each leg's
  `[api] spend:` lines from its stderr; the last one gives the row's requests, tool calls and wall
  time), `rounds.jsonl` (for each `lndl` leg, the syntax each round used, read from its
  conversation: `F` an `lndl` fence, `L` an `<lact` tag, `I` an `<invoke` tag, `O` an `OUT{`, `-`
  none of them) and `environment.json` (the lionagi checkout the run's environment installed and
  the read tree's checkout, read after the run).
- `runs/run_pilot.as-run.py.txt`: the runner exactly as it ran, byte for byte (sha256
  `911c41cd…`, the `runner_sha256` in each `header.json`).

## Runs

| run | lionagi | lndl round with no block |
|---|---|---|
| r2 | `4d373f9c` | continues with no error |
| r3 | `aca2e2e9` | a repair round that says nothing ran (ADR-0132 D2) |
| r4 | `d8838f28` | as r3; the repair round also says how a block opens and closes, the reader takes indented, longer and CRLF fences, and an empty block is a repair round |

Each run's environment had lionagi installed in editable mode from a checkout of the commit in the
table: `4d373f9c94f646fe894b111071609461c9198301`, `aca2e2e9ab3f8000af3db0dfd0f7c2206640859c` and
`d8838f28ed9b3e218dcf834ff2048c5fb016139e`. All three report version 0.35.5, which is all
`header.json` records (`runner_li`), so each run's `environment.json` records the commit, read from
its checkout after the run: the checkout's HEAD, the time its reflog last moved HEAD (before the
run's `started_utc` in each run) and its count of changed or untracked files (0 in each). The read
tree's checkout is recorded the same way. `aca2e2e9` is not on `main`: it is a commit of the branch
merged as `d8838f28` (#3640), two commits past `4d373f9c`, and the only file in the Python package
it changes from `4d373f9c` is `lionagi/operations/lndl_middle/lndl_middle.py`.

| run | loop | accepted by the checks | accepted on reading | requests | requests per accepted | tool calls | wall time |
|---|---|---|---|---|---|---|---|
| r2 | react | 23 / 24 | 24 / 24 | 94 | 3.92 | 56 | 295.9 s |
| r2 | lndl | 18 / 24 | 18 / 24 | 119 | 6.61 | 34 | 330.4 s |
| r3 | react | 22 / 24 | 23 / 24 | 95 | 4.13 | 59 | 315.9 s |
| r3 | lndl | 23 / 24 | 23 / 24 | 79 | 3.43 | 37 | 210.5 s |
| r4 | react | 23 / 24 | 24 / 24 | 91 | 3.79 | 59 | 290.7 s |
| r4 | lndl | 22 / 24 | 23 / 24 | 80 | 3.48 | 47 | 236.4 s |

Requests per accepted task divides a run's requests by the tasks accepted on reading. Wall time is
the sum of the legs' own `wall_seconds`; each run had up to four legs going at once
(`--concurrency 4`), so it is not the time the run took.

Each task's check (`accept` in `tasks.yaml`) is a first pass on its answer. Every answer in
r2, r3 and r4, accepted by its check or not, was then read against the `truth` line `tasks.yaml`
records for its task (written against `6fa69b19`), and ADR-0132 D4 takes the reading as the
decision. Read that way, r2 fails the rule, r3 meets it with no margin (23 accepted tasks on each
side), and r4, at `d8838f28`, fails it (`lndl` 23 against ReAct 24). By the checks alone the
outcomes are the same, r3 by one task. A fourth run, `r5`, ran before the reading rule was
adopted (ADR-0132 D4); its record is added in a later change, and no run recorded here decides D4.

The reading is one reader's judgment, made by the author of this change knowing which loop wrote
each answer; no blind second reading was done. It judges what each answer says to its question and did not check every side claim;
each task's `truth` line is the source line it was read against, so another reader can redo it. An
answer counts as right when it gives the value or name the `truth` line records, also when it
corrects a premise of the question on the way. Two tasks turn on that rule (source lines at
`6fa69b19`):

- `t03` asks for the default `max_tokens` the Anthropic messages endpoint gets. Five accepted
  answers say the endpoint's request model has no default of its own
  (`lionagi/providers/anthropic/messages.py:86`) and give 8192 as the value lionagi's CLI applies
  (`lionagi/cli/agent.py:89`, `:1027-1030`): the r3 ReAct and r4 `lndl` answers open with the
  missing default, and the r2 ReAct, r3 `lndl` and r4 ReAct answers open with 8192. The reading
  counts all five as right, since 8192 is the value the task records. Counted wrong, the two that
  open with the missing default make r3 `lndl` 23 against ReAct 22 and r4 `lndl` 22 against ReAct
  24; all five make r3 `lndl` 22 against ReAct 22 and r4 `lndl` 22 against ReAct 23. No outcome
  changes.
- `t05` asks for "the default max_tokens value for the BashTool's timeout field in milliseconds";
  the field holds a timeout, not a token count. All six answers give 30000 ms, the value the task
  records (`lionagi/tools/code/bash.py:35`). Four of them also say the question's `max_tokens`
  does not fit the field (the r2 ReAct, r2 `lndl`, r3 ReAct and r4 ReAct answers), and the r3
  `lndl` answer says "It is a time limit, not a token count." Counted wrong, those four leave r2
  failing the rule and r3 meeting it (`lndl` 23 against ReAct 22), and make r4 meet it (23 against
  23); counting the r3 `lndl` answer wrong too leaves r3 at 22 against 22, which still meets it.

r4 fails the rule by one task, so counting any one of its accepted ReAct answers wrong, such as its
`t05` answer, would make it meet the rule.

The reading and the checks differ in ten answers, the `reading.jsonl` rows whose `check` and
`reading` disagree:

| task | runs | loop | check | reading | why |
|---|---|---|---|---|---|
| `t06` | r2, r3, r4 | both | rejected | right | The check expects `100000`. Each answer writes the cap as `100_000`, all but the r4 ReAct answer also as `100,000`, and names `_MAX_OUTPUT_BYTES` and `_SHELL_CONTROL` (`lionagi/tools/_subprocess.py:20`, `:18`). |
| `t20` | r4 | `lndl` | rejected | right | The question asks which module defines the class; the answer names it by its import path, `lionagi.tools.code.search`, and says it did not open the file. The check expects the file path; the class is defined at `lionagi/tools/code/search.py:193`. |
| `t24` | r2, r3, r4 | `lndl` | accepted | wrong | Each answer gives `RUNS_ROOT` in place of `RUNS_DIR`: r3 says no `RUNS_DIR` exists, r2 that its grep shows none in `lionagi/mcp/config.py`, and r4 that the module defines none, adding that it read the file only in part and does not rule one out. Line 19 of that file is `RUNS_DIR = RUNS_ROOT`. The check asks for `.lionagi`, the default the question asks for, and the names `RUNS_DIR` and `JOBS_DIR`, which the question itself supplies, so it accepts these answers. |

The other rejected answers are not right on reading either: the five r2 `lndl` legs that spent all
11 requests (`t03`, `t04`, `t17`, `t18`, `t19`; ADR-0132 D4, Result) give no value, each saying it
could not confirm, list or determine what was asked, and the r3 ReAct `t04` leg made no tool call
and answered that none of its tool calls returned results. Three accepted
answers carry an error in a side claim that does not touch what the question asks (source lines
at `6fa69b19`): the r4 `lndl`
answer to `t22` says the question gives `_wire_secure_guards` the signature
`(spec: AgentSpec, hooks: dict)`, which the question does not (the rest of the answer matches
`lionagi/agent/spec.py:48-56`); the r2 ReAct answer to `t05` says `BashTool.call` substitutes the
default, where the method is `handle_request` (`lionagi/tools/code/bash.py:111`); and the r4
`lndl` answer to `t03` says the CLI applies the fallback with `.get`, where it calls `setdefault`
(`lionagi/cli/agent.py:1028`). The r2 ReAct answer to `t21` says `_wire_secure_guards` is
"imported from lionagi/agent/spec.py line 44"; that line is where the function is defined, and the
import is at `lionagi/cli/agent.py:266`. The reading does not count it as a fourth, since the module
and line it names are the definition the task records and only the verb is loose.

The committed `tasks.yaml` is the file the runs used, unchanged. Each task also has a `none_of: []`
beside `accept`, which the runner does not read; it is left as it was so that `tasks.yaml` still
matches the hash in each `header.json`. Two `enumerate_known_member` lines in it are one line off
at `6fa69b19`, and no check reads them: `t13`'s `system` is at
`lionagi/protocols/messages/base.py:24`, and `t19`'s `.env` at `lionagi/libs/path_safety.py:12`.

`run_pilot.py` takes each leg's requests from its spend line and counts a leg with none as zero
requests. `summary.json` records how many legs that was (`missing_spend`). A leg counted as zero
lowers its loop's requests, so the runner computes the D4 rule only while that number is 0 for both
loops; otherwise it writes `d4_rule` as `null` and says why in `d4_rule_unset`. It is 0 in r2, r3
and r4, and `spend_lines` is at least 1 in all 144 rows. Each leg's spend lines are in its run's
`spend.jsonl`.

## How the committed files differ from the runs' output

- `run_pilot.py` is `runs/run_pilot.as-run.py.txt` with `noqa` comments added, the `(r1)` at the
  end of one comment removed (the as-run copy keeps it), the layout reformatted by ruff, and three
  changes. First, `summarize()` leaves the D4 rule unset when a leg has no spend line (above) or
  the rows are not the whole run, where the as-run copy computed it. Second, a run that stops early
  keeps what it has (`run_one()`, `run_pair()`, `main()`): a leg that raises in the runner, in its
  `subprocess.run` or after it (writing its output, or running its check), becomes a row with `rc`
  `harness-error` and no spend line; a leg that times out keeps its output so far, and bytes that do
  not decode are replaced in any leg's output; each row is appended to `results.jsonl` as its leg
  ends, and the summary and table count it only once it is written there (a row that cannot be
  written stops its task on a harness error); and an
  interrupt starts no further leg and still writes the summary and table (below). Third, `main()`
  makes the run directory only after it has checked that the read tree exists and `li` exists and
  is executable, read the task file, checked each task (`task_problems()`: an `id` of letters,
  digits, `_` and `-` that does not repeat, a non-empty `class` and `prompt`, checks that are
  lists of non-empty strings with a non-empty `all_of`, each regular expression compiling) and
  checked `--concurrency`, `--loops` and `--only`, so none of those failing leaves a run directory
  behind; a failed check exits 2. Of the two files' top-level statements,
  `summarize()`, `run_one()`, `run_pair()` and `main()` are the ones whose `ast.dump` differs, and
  `run_pilot.py` adds two imports (`threading` and `collections.abc.Callable`), the `TASK_ID`
  pattern and `task_problems()`. With `missing_spend` at 0 and every leg run in all three runs, both
  write the same `summary.json`; `tests/benchmarks/test_lndl_pilot.py` recomputes each committed
  summary from its run's `results.jsonl`.
- In each `header.json`, `tasks_file` was an absolute path on the machine that ran it and now
  reads `tasks.yaml`. Nothing else in `runs/` was changed, apart from a final newline added to the
  JSON files; `answers.jsonl`, `reading.jsonl`, `spend.jsonl`, `rounds.jsonl` and
  `environment.json` were added. `tests/benchmarks/test_lndl_pilot.py` checks that each
  `reading.jsonl` has one row for each row of its run, that its `check` column is the run's
  `accepted`, and that the counts in ADR-0132's Result table are its tally.
- The runner also writes each leg's prompt, stdout and stderr beside its results, and `li agent`
  keeps each leg's conversation under the run's `home/`. Of those, the stdout is committed as
  `answers.jsonl`, the stderr's spend lines as `spend.jsonl`, and the syntax of each round of each
  `lndl` conversation as `rounds.jsonl`, from which ADR-0132 counts the runs that opened with an
  `<invoke>` tag and the requests that went to rounds with no block. The prompts, the rest of the
  stderr and the conversations are not committed.

## Running it

The runner expects, beside it, `tree/` (a checkout of `6fa69b19`), `venv/` (an environment with
the lionagi to measure installed, so `venv/bin/li` exists), `home/` (an empty directory: the
runner sets `HOME` to it and `LIONAGI_HOME` to `home/.lionagi`, so the configuration in the real
home directory is not read) and `ANTHROPIC_API_KEY` in its environment. Each leg runs in `tree/`,
and `li agent` looks for its profile in a `.lionagi/agents/` in `tree/` and then in each directory
above it, which is how it finds the profiles beside the runner; a profile of the same name in a
directory nearer to `tree/` would be taken first. Then:

```bash
venv/bin/python run_pilot.py --tasks tasks.yaml --out runs/<name>
```

A run is 48 `li agent` legs, two per task. `--only t01,t02` runs part of the task set. A run that
does not cover every leg of the whole task set, because of `--only`, an interrupt, or a task that
stopped on an error in the runner itself, still writes `summary.json` and `table.md` from the rows
it has: `summary.json` says why under `partial`, the D4 rule is `null` with the same reason in
`d4_rule_unset`, and `table.md` has a line for every task the run covers, marking a leg that did not
run as `not run`. On an interrupt (Ctrl-C)
no further leg starts and the runner waits for the legs already running. A second interrupt stops
that wait: the summary and table are written from the rows so far, `partial` says that rows of
legs still running may follow in `results.jsonl`, and the process exits once those legs end. The
runner exits 2, with no run directory, when the read tree or `li` is missing; 130 after an
interrupt; and 1 when a task stopped, or a leg raised, on an error in the runner itself.
