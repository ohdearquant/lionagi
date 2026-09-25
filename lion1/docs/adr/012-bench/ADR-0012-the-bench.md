---
adr: ADR-0012
status: draft
liveness: operating (the set runner, the in-box grader, the closure, the run identity and both arms are under tests; that compared bench runs share instances, head and budgets is the reader's check; no CLI-arm bench run on the hard set has the package indexes closed)
date: "2026-09-25"
area: bench
kind: new
depends_on:
  - "[[ADR-0002-the-run|ADR-0002]]"
  - "[[ADR-0003-the-bounds|ADR-0003]]"
  - "[[ADR-0006-the-notification|ADR-0006]]"
  - "[[ADR-0009-backends|ADR-0009]]"
tags:
  - adr
  - bench
---

# ADR-0012: The bench as the instrument

## Context

A change to the loop needs a number that comes from a repository and a grader, never from the
model's word, and a second harness on the same model to read it against. The bench is that number.
Each SWE-bench Verified instance runs in a box started from its own evaluation image; the patch is
the tree's diff when the work ends; the official harness's steps grade it in that box. The
comparison arm runs a coding CLI inside the same box, as the whole harness, on the same model.

This record fixes what the bench measures, what it keeps per instance, how a bench run is named and
repeated, what may be read against what, and why the number is unaided. The box, its tools and the
watch over the tree are [[ADR-0011-the-box|ADR-0011]]'s (the box); the backend and its envelope are
[[ADR-0009-backends|ADR-0009]]. Source: `bench/swe/run_set.py`, `bench/swe/run_swe.py`,
`bench/swe/codex_cli.py` and `bench/swe/sandbox.py`; `bench/swe/run_naked.py` for the older
host-side baseline.

## Definitions

- **instance**: one SWE-bench Verified task: an issue, the repository at its base commit inside an
  evaluation image, and the eval script that runs the hidden tests.
- **set**: a named file of instances the runner reads: Mini-50 (25 django and 25 sphinx instances),
  the 500 Verified, or a local few. A launch narrows it by repository, instance ids or a limit.
- **arm**: who works the instance: the loop through a backend, or the coding CLI as the whole
  harness inside the box.
- **bench run**: everything launched under one run id: one arm, one model name, one access policy,
  one row per instance, resumable across launches.
- **row**: what the bench keeps for one instance: outcome, turns, calls, tokens, cost, the grade,
  `resolved`, and the fields that name its bench run.
- **ledger**: a bench run's append-only file of rows, one JSON line per instance; an instance
  already in it is not run again.
- **manifest**: one launch's record, written before the first box: its arguments, the source head
  and dirty files, the set's digest, and the instances it selected.
- **unaided**: a bench run whose boxes cannot reach the code hosts and package indexes a published
  fix would come from.
- **resolved**: the verdict the official harness's report function returns for a patch on its
  instance.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | The official harness's steps, run in the instance's own box, give the verdict its own container would. | `sandbox.grade` runs the steps of the harness's `run_instance`: reset, three apply attempts, the eval script, the report function (assumption beyond those steps) | `resolved` is this grader's count, and a regrade in the harness's container is owed |
| A2 | The model a bench run names is the model that served it, in both arms. | the loop names the model id and each call keeps its serving provider; the CLI arm names a router preset whose content the router holds ([[ADR-0009-backends\|ADR-0009]] S5) | the arms measure two models, and the served model is read from the responses before any comparison |
| A3 | Two Mini-50 instances cannot pass from a box. | sphinx-8269 and sphinx-8475 are unresolved in all 14 full Mini-50 bench runs, both arms; their grader logs show a `www.w3.org` fetch reset | both arms' ceiling is 50, not 48, and every Mini-50 count understates its arm |

## Claims

### C1: The number is the official grade of the tree's diff, taken in the box it was written in _(enforced: mechanical)_ ^c1

- **Subject**: every row a bench run grades, both arms.
- **Violated when**: `resolved` comes from anything but the harness's report on the box's diff, a
  tree whose reset failed is graded, or a patch whose writer was not confirmed stopped is graded.

The patch is the box's diff when the arm ends, whatever the model or the CLI said about it. The
grader resets the tree to the base commit, applies the patch with the harness's three attempts, runs
the instance's eval script, and reads the log with the harness's own report function.

The row keeps the verdict and a status: graded, timeout, apply failed, reset failed, empty patch, or
execution uncertain. An empty patch is not graded. A CLI exec that failed in transport carries no
patch, because a command it started may still be writing the tree.

### C2: A row says what the instance cost, and a spend nobody measured is unknown, never zero _(enforced: mechanical)_ ^c2

- **Subject**: every row, both arms, and the bench run's summary.
- **Violated when**: an unreported spend reads as a complete number, a dead box's attempt drops its
  spend, or a failed call counts as a turn.

A loop row carries the outcome ([[ADR-0002-the-run#^c4|ADR-0002/C4]]) and what stopped it, turns
(the code's `rounds`), and one entry per call from its envelope
([[ADR-0009-backends#^c5|ADR-0009/C5]]): time, serving provider, attempts, tokens, finish reason,
cost. Token sums follow, then `cost_usd` as the known subtotal beside `cost_complete`.

A box that dies is replaced once, and the first attempt's spend stays on the row. The summary adds
only known cost and counts the rows that are not whole. The ledger keeps the row without its calls
and patch; the instance's own file keeps both.

### C3: A run id is one bench run, and a launch that disagrees is refused before anything is written _(enforced: mechanical)_ ^c3

- **Subject**: every launch of the set runner.
- **Violated when**: a launch with another access policy, model, backend or harness joins a run id,
  a ledger row not stamped with those fields is counted, or a manifest overwrites another.

The run id's `run.json` holds the four fields that make two launches one bench run. Manifests
written before it must agree and name all four; a row without the stamp refuses the launch. A
resumed launch skips the instances its ledger holds.

Each launch creates its own manifest: arguments, head and dirty files, the set's path and digest,
the instances selected after every filter, and for the CLI arm its pinned version and configuration.
The head is not one of the four: a resumed bench run may continue at another head, and each manifest
says which.

### C4: The bench runs unaided unless a launch says otherwise _(enforced: mechanical)_ ^c4

- **Subject**: every box a set runner launch starts, both arms.
- **Violated when**: the model's work starts in a box where a probed host resolves beyond loopback
  or answers a fetch, the grader runs with the closure in place, or a row does not say whether it
  was aided.

The runner points the code hosts and package indexes at loopback in the box's `/etc/hosts`, both
address families, then proves it from inside: `github.com` and `pypi.org` must resolve only to
loopback and an HTTPS fetch of each must fail. Otherwise the instance's row records the error and no
work starts.

Both arms' prompt says the run is unaided and the published fix out of bounds. The CLI arm installs
its binary before the closure. The lines are removed before grading, since an eval script may
install. `--aided` leaves the hosts open; every row carries `aided`. Only the named hosts close.

### C5: Unaided is the number; an aided number measures access to the answer _(enforced: process)_ ^c5

On the 45 Verified instances rated one hour or more, with the code hosts open, the CLI arm resolved
44 and the loop 29; the runner's help text records 44 and 14 of those runs fetching the published
fix. With the code hosts closed, the same instances resolved 21 and 24.

Those first closed bench runs could still reach the package indexes, and 8 loop runs and 4 CLI runs
asked them for a later release; their rows carry no `aided` field. With both closed, on the box's
tools in place of the bench's earlier commands, the loop resolved 16 of 45 in each of two bench
runs; the drop is not attributed to either change. The gate: a count quoted as a harness result
names bench runs whose rows say `aided: false`.

### C6: The settle and criteria stops are off by default; the gate on `OUT{}` is always on _(enforced: mechanical)_ ^c6

- **Subject**: every loop-arm run the set runner starts.
- **Violated when**: a run stops on settle or on its own checks without `--settle` or `--criteria`
  in its manifest, or an `OUT{}` the watch's gate refuses is accepted.

The watch's line in the notification asks for `OUT{}` or a named gap once the fix has stood three
turns unchanged. The gate, the job's `accept` ([[ADR-0003-the-bounds#^c4|ADR-0003/C4]]), refuses a
finish with no change, a change only in tests or new files outside the tracked tree, a change
nothing ran against, or a failing declared check.

`--settle N` stops a run after N unchanged turns: on the aided hard set it stopped 31 of 45 loop
runs, 12 unresolved. `--criteria` ends the run when the model's checks pass. On Mini-50 it lost: 26
against 32 of 50 with thinking off, 35 against 41 with a 2000-token budget, where 27 runs ended on
their checks and 6 of those failed the grader.

### C7: The router arm thinks under a 2000-token budget and stops at the notification frame _(enforced: process)_ ^c7

The loop arm's router backend runs the model's own thinking under a 2000-token budget and ends each
completion where the model starts writing the loop's own frame, `<system round=`. On Mini-50 that
setting resolved 42 of 50 against 33 with thinking off, both with the stop; unbounded thinking
without the stop resolved 35.

Without the stop a thinking run could spend three to eight turns of 32k tokens on an imagined
continuation and time out with no patch. The provider treats the budget as soft: calls reach twice
it. The gate: `--thinking` in the manifest names any other setting, and a count read against these
rows names its own.

### C8: Bench runs are read against each other on the same instances, head and budgets _(enforced: process)_ ^c8

Two bench runs are compared only on the same instances, the same model name and the same budgets,
and a change to the loop is read against the bench run before it, with the head each ran at. The
manifests carry all of it; the runner checks none of it across run ids.

The recorded unaided pair on the hard set shares one head but ran the loop under a 2400 s budget and
the CLI under 3600 s. A CLI arm's `rounds` counts the commands it ran, and its calls are one entry
per exec, so turns and calls compare within an arm only. The gate: a quoted comparison names both
bench runs and states every difference their manifests show.

### C9: The figures other records cite belong to named bench runs, never to the Mini-50 table _(enforced: process)_ ^c9

Every Mini-50 bench run below kept the code hosts open. The loop's ran with the settle rule on and
the bench's earlier commands, `run`, `read_file`, `grep`, `list_dir` and `edit` among them. A figure
is cited with its bench run.

| bench run on Mini-50 | resolved | turns | figures cited from it |
| --- | --- | --- | --- |
| the CLI arm in the box | 45 of 50 | not counted as turns | none |
| the loop, 2000-token thinking with the frame stop | 42 of 50 | 952 | 1976 calls and 3 output field errors ([[ADR-0001-the-actor#Assumptions\|ADR-0001]] A1), the calls not reproduced: the record dumps hold 1,775 settled results; 52 turns unreadable and 9 context commands ([[ADR-0004-the-language\|ADR-0004]]) |
| the loop, another bench run | 37 of 50 | 837 | 8 edit errors ([[ADR-0005-command-handling#Assumptions\|ADR-0005]] A1) |
| the loop, another bench run | 42 of 50 | 765 | 4 edit errors ([[ADR-0005-command-handling#Assumptions\|ADR-0005]] A1) |

## Decisions

### D1: The set runner in `bench/swe/run_set.py` ^d1

Serves C1 to C5. One box per instance from its evaluation image, local or remote, `--parallel` wide.
The hosts close unless `--aided`; the arm runs; the hosts reopen; the box grades the patch and the
row is appended under a lock. A box that dies gets one fresh box. Defaults: unaided, settle 0,
criteria off, a 1200 s time budget, 100 turns as a backstop, a cost cap only on a metered arm.

- **Landing evidence**: `tests/test_swe_run_set.py`: the retried spend kept, a cap on an unmetered
  arm refused before any box, an unconfirmed stop not graded, a disagreeing resume refused, a
  matching resume skipping ledger rows, manifests never overwritten.

### D2: The grader in `bench/swe/sandbox.py` ^d2

Serves C1 and C4. `grade` mirrors the harness's `run_instance` in the box: reset, three apply
attempts, the eval script under a timeout, the harness's report function, a status on every early
stop. `close_code_hosts` writes marked `/etc/hosts` lines and probes from inside; `open_code_hosts`
rewrites the file in place, since it is a bind mount, and proves no marked line is left.

- **Landing evidence**: `tests/test_swe_sandbox.py`: `test_failed_reset_is_not_graded`, the probe
  test refusing a reachable or elsewhere-resolving host, the ledger stamp test.

### D3: The loop arm in `bench/swe/run_swe.py` ^d3

Serves C2, C6 and C7. `solve` gives the model the box's tools, one fixer profile and the issue, and
adds the watch's section and a section naming a turn cut at the output cap or blank. It asks stop on
a dead box, the watch and the cost cap, then harvests every call. `--criteria`, `--tdd` and
`--deliberate` change the profile's text.

- **Landing evidence**: `tests/test_swe_solve.py`: rounds and patch kept on a backend failure,
  missing usage incomplete, a failed envelope not a turn, a dead box ending the run;
  `test_the_thinking_flag_maps_to_the_reasoning_field`.

### D4: The CLI arm in `bench/swe/codex_cli.py` ^d4

Serves C1, C2 and C4. The arm installs a pinned CLI release in the box, writes a private
configuration, and runs it on the same task text with a kill at the time budget. The key rides only
in the exec environment. Tokens come from its turn events, priced at a dated rate table; an unpriced
model has no cost.

- **Landing evidence**: `tests/test_swe_codex_cli.py`: the key in no artifact, the budget kill still
  keeping the diff, a transport error taking no patch, missing usage unknown, a reported zero a
  complete zero.

## Alternatives

| approach | rejected because |
| --- | --- |
| Grading in the harness's own container after the run | a second container and an image round trip per instance; the same steps run in the box the patch was written in |
| Taking the model's `Done` as the result | the patch is what the tree holds; the gate checks a finish and the grader grades the diff |
| Keeping aided counts as harness results | with the hosts open the CLI arm resolved 44 of 45, and the runner records 44 of its runs fetching the published fix |
| Closing the box's whole network | the CLI arm's model calls leave from inside the box; only the answer's sources close |
| Ending on the model's own checks by default | 26 against 32 and 35 against 41 of 50 on Mini-50 |
| Stopping on settle by default | on the aided hard set it stopped 31 of 45 loop runs, 12 of them unresolved |

## Consequences

- **S1**: No Mini-50 count here is an unaided result; they compare arms and settings with the hosts
  open.
- **S2**: No CLI-arm bench run on the hard set has the package indexes closed, and its one closed
  run had a longer time budget than the loop's; that pair is owed before the arms are compared
  unaided.
- **S3**: The CLI arm's cost is its token counts priced at a table in `bench/swe/codex_cli.py`; the
  loop's is what the router reported per call. They are two instruments, and a cost ratio across
  arms names both.
- **S4**: The sets and the results are outside version control; a bench run is repeated from its
  manifest's set digest, head and arguments. How a set file is drawn is not in the code.
- **S5**: Mini-50 does not exercise the context layer: the 952-turn bench run issued 9 context
  commands.
- **S6**: The host-side baseline in `bench/swe/run_naked.py` runs the CLI with its own tools, one
  whole task per call (`ClaudeCodeAgentic`), over a copied tree and grades with the harness's own
  evaluation. It has no ledger, manifest or closure, and it writes an unreported cost as zero.
- **S7**: A CLI row's `outcome` is read off the exec's exit code and names no run outcome; only a
  loop row's is one.
- **S8**: The boundary: what any instrument declares, returns and reports when it fails, and who
  hears it, is the instrument contract ([[ADR-0014-the-instrument|ADR-0014]], the instrument
  contract). The bench is one instrument: it returns rows and a ledger, never a run's outcome.
  Asking for a bench run by mail belongs to the long-running agent
  ([[ADR-0013-the-agent|ADR-0013]]).
