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
  come from the `[api] spend:` line `li agent` prints (ADR-0132 D3); acceptance is the task's own
  check.
- `runs/r2/`, `runs/r3/`, `runs/r4/`: `header.json` (hashes of the runner, task set and profiles, the read
  tree's commit, the `li` version), `results.jsonl` (one row per run), `summary.json` (totals and
  the D4 rule) and `table.md` (the per-task table).

## Runs

| run | lionagi | lndl round with no block |
|---|---|---|
| r2 | `4d373f9c` | continues with no error |
| r3 | `aca2e2e9` | a repair round that says nothing ran (ADR-0132 D2) |
| r4 | `d8838f28` | as r3, and the repair round also says how a block opens and closes |

| run | loop | accepted | requests | requests per accepted | tool calls | wall time |
|---|---|---|---|---|---|---|
| r2 | react | 23 / 24 | 94 | 4.09 | 56 | 295.9 s |
| r2 | lndl | 18 / 24 | 119 | 6.61 | 34 | 330.4 s |
| r3 | react | 22 / 24 | 95 | 4.32 | 59 | 315.9 s |
| r3 | lndl | 23 / 24 | 79 | 3.44 | 37 | 210.5 s |
| r4 | react | 23 / 24 | 91 | 3.96 | 59 | 290.7 s |
| r4 | lndl | 22 / 24 | 80 | 3.64 | 47 | 236.4 s |

r2 fails the rule, r3 passes it, and r4, at the merged code, fails it by one accepted task
(`t20`). ADR-0132 D4 reads them together.

`t06`'s check expects `100000`. Every answer to it in r2 and r3, in both loops, wrote `100,000`,
and in r4 the ReAct answer wrote `100_000` and the `lndl` answer `100,000`, so it is rejected in
all six. With that one check read as
accepting the answer, r2 is react 24, lndl 19, r3 is react 23 at 4.13 requests per accepted task,
lndl 24 at 3.29, and r4 is react 24 at 3.79, lndl 23 at 3.48; no run's outcome changes. The committed `tasks.yaml` is the file the runs used, unchanged.

## What was changed for this directory

- `run_pilot.py` is the runner that produced the runs (sha256 `911c41cd…`, recorded in each
  `header.json`), with `noqa` comments added and the layout reformatted by ruff. Its syntax tree
  is identical to the original's.
- In each `header.json`, `tasks_file` was an absolute path on the machine that ran it and now
  reads `tasks.yaml`. Nothing else in `runs/` was changed, apart from a final newline added to the
  JSON files.

## Running it

The runner expects, beside it, `tree/` (a checkout of `6fa69b19`), `venv/` (an environment with
the lionagi to measure installed, so `venv/bin/li` exists), `home/` (an empty directory: the
runner sets `HOME` to it and `LIONAGI_HOME` to `home/.lionagi`, so no user configuration is read)
and `ANTHROPIC_API_KEY` in its environment. Then:

```bash
venv/bin/python run_pilot.py --tasks tasks.yaml --out runs/<name>
```

A run is 48 `li agent` legs, two per task.
