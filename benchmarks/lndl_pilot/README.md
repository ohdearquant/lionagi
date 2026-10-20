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
- `run_pilot.py`: the runner. Each task runs once per loop; the two runs of a task are adjacent in
  time and their order alternates by task, four tasks in flight. Requests, tool calls and wall time
  come from the `[api] spend:` line `li agent` prints (ADR-0132 D3); the task's own check is the
  first pass on acceptance, and the reading below decides.
- `runs/r2/`, `runs/r3/`, `runs/r4/`, `runs/r5/`: `header.json` (hashes of the runner, task set and
  profiles, the read tree's commit, the `li` version), `results.jsonl` (one row per run),
  `summary.json` (totals and the D4 rule), `table.md` (the per-task table) and `answers.jsonl` (each
  run's answer, the leg's stdout with surrounding whitespace removed, as the check read it; its
  length is that row's `answer_chars`).
- `runs/run_pilot.as-run.py.txt`: the runner exactly as r2, r3 and r4 ran it, byte for byte
  (sha256 `911c41cd…`, the `runner_sha256` in their `header.json`).
- `runs/r5/run_pilot.as-run.py.txt`: the runner exactly as r5 ran it, byte for byte (sha256
  `46234f89…`, r5's `runner_sha256`). It differs from `run_pilot.py` in one comment, and all three
  copies parse to the same syntax tree.
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
`d8838f28ed9b3e218dcf834ff2048c5fb016139e` and `208bcc08dbdc512f31c142abf69c256f79a6536a`. Each
checkout was moved to its commit before its run started and has not moved since, and its working
tree reads clean. All four report version 0.35.5, which is all `header.json` records (`runner_li`),
so the commits are recorded here, read from those environments after the runs.

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

Each task's check (`accept` in its task file) is a first pass on its answer. Every answer in r2
to r5, accepted by its check or not, was then read against the `truth` line its task records (at
`6fa69b19`, the tree the runs read), and ADR-0132 D4 takes the reading as the decision. Read that
way, r2 fails the rule, r3 meets it with no margin (23 accepted tasks on each side), and r4 fails
it (`lndl` 23 against ReAct 24). r5, the run that decides D4, fails it: `lndl` 22 at 3.59 requests
per accepted task against ReAct 23 at 4.30. By its checks r5 meets the rule with 23 on each side;
the difference is the `lndl` answer to `t24`.

In r2, r3 and r4 the reading and the checks differ in ten answers, and in r5 in one (r5's `t06`
and `t20` checks take the answers the earlier ones rejected):

| task | runs | loop | check | reading | why |
|---|---|---|---|---|---|
| `t06` | r2, r3, r4 | both | rejected | right | The check expects `100000`. Each answer writes the cap as `100_000`, all but the r4 ReAct answer also as `100,000`, and names `_MAX_OUTPUT_BYTES` and `_SHELL_CONTROL` (`lionagi/tools/_subprocess.py:18`, `:20`). |
| `t20` | r4 | `lndl` | rejected | right | The question asks which module defines the class; the answer names it by its import path, `lionagi.tools.code.search`, and says it did not open the file. The check expects the file path `lionagi/tools/code/search.py`. |
| `t24` | r2, r3, r4, r5 | `lndl` | accepted | wrong | Each answer says `lionagi/mcp/config.py` defines no `RUNS_DIR`; line 19 of that file is `RUNS_DIR = RUNS_ROOT`. The check asks for the names `RUNS_DIR` and `JOBS_DIR`, which the question itself supplies. |

The other rejected answers are wrong on reading too: the five r2 `lndl` legs that spent the round
budget (`t03`, `t04`, `t17`, `t18`, `t19`; ADR-0132 D4, Result) and the r3 ReAct `t04` leg, which
made no tool call and answered that none of its tool calls returned results. So are r5's two:
the `lndl` answer to `t03` gives `32768` from another package in the read tree, where lionagi's
CLI default is `8192` (`lionagi/cli/agent.py:89`), and the ReAct leg on `t10` called a tool named
`grep`, which is not one of its read tools (`reader`, `search`, `context`), and then answered that
it could not find the constants. One accepted answer
carries an error that does not touch what the question asks: the r4 `lndl` answer to `t22` says
the question gives `_wire_secure_guards` the signature `(spec: AgentSpec, hooks: dict)`, which the
question does not; the rest of the answer matches `lionagi/agent/spec.py:48-56`.

### r5, task by task

Each row gives the source line its reading used and, per loop, what the check did and what the
reading found; `tasks.yaml` records the rest of each task's truth. The lines are cited at
`6fa69b19`, the tree the legs read: of the 32 source lines `tasks.yaml` records, ten hold different
text at `208bcc08`, the code r5 ran.

| task | source line | `lndl` | ReAct |
|---|---|---|---|
| `t01` | `lionagi/_spec_limits.py:11` | accepted, right | accepted, right |
| `t02` | `lionagi/version.py:1` | accepted, right | accepted, right |
| `t03` | `lionagi/cli/agent.py:89` | rejected, wrong | accepted, right |
| `t04` | `lionagi/cli/agent.py:87` | accepted, right | accepted, right |
| `t05` | `lionagi/tools/code/bash.py:35` | accepted, right | accepted, right |
| `t06` | `lionagi/tools/_subprocess.py:20` | accepted, right | accepted, right |
| `t07` | `lionagi/tools/file/reader.py:24` | accepted, right | accepted, right |
| `t08` | `lionagi/cli/agent.py:83` | accepted, right | accepted, right |
| `t09` | `lionagi/agent/permissions.py:83-88` | accepted, right | accepted, right |
| `t10` | `lionagi/hooks/bus.py:71` | accepted, right | rejected, wrong |
| `t11` | `lionagi/tools/coding.py:286-294` | accepted, right | accepted, right |
| `t12` | `lionagi/dispatch/outbox.py:48` | accepted, right | accepted, right |
| `t13` | `lionagi/protocols/messages/base.py:21-28` | accepted, right | accepted, right |
| `t14` | `lionagi/protocols/generic/event.py:30-39` | accepted, right | accepted, right |
| `t15` | `lionagi/session/control.py:13-17` | accepted, right | accepted, right |
| `t16` | `lionagi/service/providers.py:34-43` | accepted, right | accepted, right |
| `t17` | `lionagi/work/form.py:40` | accepted, right | accepted, right |
| `t18` | `lionagi/engines/__init__.py:10-59` | accepted, right | accepted, right |
| `t19` | `lionagi/libs/path_safety.py:11-13` | accepted, right | accepted, right |
| `t20` | `lionagi/agent/factory.py:512-519` | accepted, right | accepted, right |
| `t21` | `lionagi/cli/agent.py:265-271` | accepted, right | accepted, right |
| `t22` | `lionagi/agent/spec.py:48-56` | accepted, right | accepted, right |
| `t23` | `lionagi/mcp/config.py:35` | accepted, right | accepted, right |
| `t24` | `lionagi/mcp/config.py:19` | **accepted, wrong** | accepted, right |

`t20`'s `lndl` answer names the module by its import path and says it did not open the file; the
question asks for the module, so it is right.

After r5, `tasks.yaml`'s `t24` check was changed to require the assignments themselves
(`RUNS_DIR = RUNS_ROOT` and `JOBS_DIR = LIONAGI_HOME / ...`, as regular expressions) instead of the
two names the question supplies. Run against the committed answers, it rejects all four `lndl`
answers (r2 to r5) and accepts all four ReAct ones; no run has used it yet.

Each task also has a `none_of: []` beside `accept`, which the runner does not read; it is left as
it was so that each task file still matches the hash in its runs' `header.json`.

`run_pilot.py` takes each leg's requests from its spend line and counts a leg with none as zero
requests. `summary.json` records how many legs that was (`missing_spend`), and the D4 rule it
computes does not depend on that number. It is 0 in r2 to r5, and `spend_lines` is at least 1 in
all 192 rows.

## What was changed for this directory

- `run_pilot.py` is `runs/run_pilot.as-run.py.txt` with `noqa` comments added, the `(r1)` at the
  end of one comment removed, and the layout reformatted by ruff. The two parse to the same syntax
  tree: `ast.dump(ast.parse(...))` of each file gives identical output.
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
