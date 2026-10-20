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
  written against commit `6fa69b19`, and the runs read a checkout of that commit. r5 ran this file;
  r2, r3 and r4 ran `runs/tasks.r2-r4.yaml`, which differs from it only in the `t06` and `t20`
  checks.
- `.lionagi/agents/d4-react.md`, `d4-lndl.md`: the profile pair, `anthropic/claude-haiku-5-5`,
  read tools, `max_extensions: 10`, `timeout: 300`.
- `run_pilot.py`: the runner. Each task runs once per loop; the two runs of a task are adjacent in
  time and their order alternates by task, four tasks in flight. Requests, tool calls and wall time
  come from the `[api] spend:` line `li agent` prints (ADR-0132 D3); acceptance is the task's own
  check.
- `runs/r2/`, `runs/r3/`, `runs/r4/`, `runs/r5/`: `header.json` (hashes of the runner, task set and
  profiles, the read tree's commit, the `li` version), `results.jsonl` (one row per run),
  `summary.json` (totals and the D4 rule), `table.md` (the per-task table) and `answers.jsonl` (each
  run's answer, the leg's stdout with surrounding whitespace removed, as the check read it; its
  length is that row's `answer_chars`).
- `runs/run_pilot.as-run.py.txt`: the runner exactly as r2, r3 and r4 ran it, byte for byte
  (sha256 `911c41cd…`, the `runner_sha256` in their `header.json`).
- `runs/r5/run_pilot.as-run.py.txt`: the runner exactly as r5 ran it, byte for byte (sha256
  `46234f89…`, r5's `runner_sha256`). It is `run_pilot.py` with one comment that has since
  changed, so all three copies parse to the same syntax tree.
- `runs/tasks.r2-r4.yaml`: the task set exactly as r2, r3 and r4 ran it, byte for byte (sha256
  `3c29864d…`, the `tasks_sha256` in their `header.json`).

## Runs

| run | lionagi | lndl round with no block |
|---|---|---|
| r2 | `4d373f9c` | continues with no error |
| r3 | `aca2e2e9` | a repair round that says nothing ran (ADR-0132 D2) |
| r4 | `d8838f28` | as r3; the repair round also says how a block opens and closes, the reader takes indented, longer and CRLF fences, and an empty block is a repair round |
| r5 | `208bcc08` | as r4 |

Each run's environment had lionagi installed in editable mode from a checkout of the commit in the
table: `4d373f9c94f646fe894b111071609461c9198301`, `aca2e2e9ab3f8000af3db0dfd0f7c2206640859c`,
`d8838f28ed9b3e218dcf834ff2048c5fb016139e` and `208bcc08dbdc512f31c142abf69c256f79a6536a`. Each
checkout was moved to its commit before its run started and has not moved since, and its working
tree reads clean. All four report version 0.35.5, which is all `header.json` records (`runner_li`),
so the commits are recorded here, read from those environments after the runs.

| run | loop | accepted | requests | requests per accepted | tool calls | wall time |
|---|---|---|---|---|---|---|
| r2 | react | 23 / 24 | 94 | 4.09 | 56 | 295.9 s |
| r2 | lndl | 18 / 24 | 119 | 6.61 | 34 | 330.4 s |
| r3 | react | 22 / 24 | 95 | 4.32 | 59 | 315.9 s |
| r3 | lndl | 23 / 24 | 79 | 3.43 | 37 | 210.5 s |
| r4 | react | 23 / 24 | 91 | 3.96 | 59 | 290.7 s |
| r4 | lndl | 22 / 24 | 80 | 3.64 | 47 | 236.4 s |
| r5 | react | 23 / 24 | 99 | 4.30 | 65 | 307.1 s |
| r5 | lndl | 23 / 24 | 79 | 3.43 | 43 | 267.9 s |

By the checks they ran with, r2 fails the rule, r3 passes it, r4, at the merged code, fails it by
one accepted task (`t20`), and r5, at the merged code with the `t06` and `t20` checks below changed
before it started, passes it: `lndl` used 79 requests against 99 and accepted as many tasks. ADR-0132
D4 left the outcome to r5, so `lndl` is now a recommended profile setting; the default stays
`react`.

`t06`'s check expects `100000`. None of the six answers to it (one per loop in each run) contains
that string: each names `_MAX_OUTPUT_BYTES` and writes its value `100_000`, and all but the r4
ReAct answer also give the cap as `100,000`, so all six are rejected. With that one check read as
accepting the answer, r2 is react 24, lndl 19, r3 is react 23 at 4.13 requests per accepted task,
lndl 24 at 3.29, and r4 is react 24 at 3.79, lndl 23 at 3.48; no run's outcome changes. For r5 the
check is `re:\b100[,_ ]?000\b`, which takes the value in any of those forms, and both r5 answers
are accepted.

`t20` asks which module defines the class the factory imports, and its check expects the file path
`lionagi/tools/code/search.py`. The r4 `lndl` answer named the module by its import path,
`lionagi.tools.code.search`, and was rejected; no other answer to `t20` was. With that check read as
accepting the import path, r4 is react 23 at 3.96, lndl 23 at 3.48, and meets the rule; with `t06`
read the same way as well, react 24 at 3.79, lndl 24 at 3.33. r4's outcome therefore rests on
this one check, and r2's and r3's do not change under either reading. For r5 the check takes
either the file path or the import path, and both r5 answers are accepted; the `lndl` one again
names the import path and says it did not open the file.

The checks are substring checks (`all_of` and `none_of` under `accept` in `tasks.yaml`), so a wrong
answer that contains the expected words can pass. The 32 accepted answers to `t09`, `t11`, `t12` and
`t22` (both loops, all four runs), where that is easiest, were read by hand, and all are correct;
they are in `answers.jsonl`. Each task also has a `none_of: []` beside `accept`,
which the runner does not read; it is left as it was so that each task file still matches the hash
in its runs' `header.json`.

r5 rejected two answers. The `lndl` answer to `t03` gives `32768` from another package in the read
tree and is wrong for lionagi, whose CLI default is `8192`. The ReAct leg on `t10` called a tool
named `grep`, which is not one of its read tools (`reader`, `search`, `context`), and then
answered that it could not find the constants.

`run_pilot.py` takes each leg's requests from its spend line, counts a leg with none as zero
requests, and computes the D4 rule in `summary.json` without checking that every leg reported one.
Every leg in r2 through r5 did (`spend_lines` is at least 1 in all 192 rows).

## What was changed for this directory

- `run_pilot.py` is `runs/run_pilot.as-run.py.txt` with `noqa` comments added and the layout
  reformatted by ruff. The two parse to the same syntax tree: `ast.dump(ast.parse(...))` of each
  file gives identical output.
- In each `header.json`, `tasks_file` was an absolute path on the machine that ran it and now
  reads `tasks.yaml`. Nothing else in `runs/` was changed, apart from a final newline added to the
  JSON files; each run's `answers.jsonl` and r5's copy of the runner were added.
- The runner also writes each leg's prompt, stdout and stderr beside its results, and `li agent`
  keeps each leg's conversation under the run's `home/`. Of those, only the stdout is committed, as
  `answers.jsonl`. The counts in ADR-0132 of
  runs that opened with an `<invoke>` tag and of requests that went to rounds with no block are read
  from the conversations.

## Running it

The runner expects, beside it, `tree/` (a checkout of `6fa69b19`), `venv/` (an environment with
the lionagi to measure installed, so `venv/bin/li` exists), `home/` (an empty directory: the
runner sets `HOME` to it and `LIONAGI_HOME` to `home/.lionagi`, so no user configuration is read)
and `ANTHROPIC_API_KEY` in its environment. Then:

```bash
venv/bin/python run_pilot.py --tasks tasks.yaml --out runs/<name>
```

A run is 48 `li agent` legs, two per task.
