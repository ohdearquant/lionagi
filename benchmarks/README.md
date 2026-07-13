Minimal concurrency benchmarks

This folder contains a lightweight benchmark runner for lionagi.ln.concurrency.
It establishes a baseline for core patterns to help catch regressions.

How to run (concurrency)

- Asyncio backend (default):
  - `python -m benchmarks.concurrency_bench`
- Trio backend:
  - `python -m benchmarks.concurrency_bench --backend trio`

Options

- `--backend {asyncio,trio}`: Select async backend (default: asyncio)
- `--repeat N`: Repeat each scenario N times and report aggregates (default: 3)
- `--json`: Also print JSON to stdout (besides saving to file)
- `--output PATH`: Save results JSON to a custom path
- `--compare BASELINE.json`: Compare against a previous run and show deltas

How to run (ln functions)

- Asyncio backend (default):
  - `python -m benchmarks.ln_bench`
- Trio backend:
  - `python -m benchmarks.ln_bench --backend trio`

Options are the same as the concurrency runner.

Results

- Results are saved under `benchmarks/results/<timestamp>-<backend>.json`
  (concurrency) and `benchmarks/results/ln-<timestamp>-<backend>.json` (ln
  functions) by default.
- Each scenario reports min/mean/median/max (seconds) over the configured
  repeats.

Scenarios (initial set)

- gather_100_yield: 100 tasks, each yields once (sleep 0)
- bounded_map_2000_limit_100: 2000 items, async no-op mapper, limit=100
- completion_stream_1000_limit_100: 1000 awaitables streamed with limit=100
- race_first_completion_10: 10 tasks where one completes immediately
- cancel_propagation_500: 500 tasks; one fails quickly to trigger cancellation
- taskgroup_start_1000_noop: start 1000 short-lived tasks

Notes

- Fuzzy utilities benches are available:
  - `python -m benchmarks.fuzzy_bench` (JSON parsing, extraction, key matching)
  - Outputs to `benchmarks/results/fuzzy-<timestamp>.json`
- These are micro-benchmarks intended to detect relative changes, not absolute
  throughput.
- Run on a quiet machine for less noisy results. Prefer CI runners for
  consistency.

CI gating (same-machine A/B)

- `.github/workflows/benchmarks.yml` no longer compares against a frozen
  JSON recorded on a different machine at a different time. Cross-runner
  hardware and load variance made that comparison noisy enough to produce
  false regressions on an unrelated commit.
- Instead, each CI run builds a second Python environment on the *same*
  runner with lionagi installed from a baseline ref (the PR's merge-base
  with its target branch, or the previous commit on a push), then runs the
  current checkout's benchmark scripts once against that baseline install
  and once against the current install. `benchmarks/ci_compare.py` diffs
  the two same-machine result JSONs with the existing 20% threshold.
- The frozen `benchmarks/baselines/*.json` files and the
  `refresh-bench-baselines` workflow that maintained them are gone; there
  is nothing to keep in sync anymore.
- A benchmark scenario that imports a symbol not yet present in the
  baseline lionagi (e.g. a bench added alongside a brand-new API) is
  skipped for that run rather than crashing the whole script — see
  `benchmarks/_compat.py`.

Paired-in-time comparison (runner speed drift)

- Same-machine A/B still assumes the runner's own speed is constant across
  the job. It can not be: burstable-CPU credit decay, thermal throttling,
  or a noisy neighbor can make a hosted runner measurably slower (or
  faster) later in the job than earlier, with nothing to do with the code
  under test. Running a whole baseline arm (tens of seconds) and then a
  whole current arm back to back puts that much wall-clock time between
  any baseline measurement and its paired current measurement, which is
  enough for drift to look exactly like a uniform, one-directional
  "regression" across every CPU-bound scenario -- the failure mode this
  section replaces.
- Each suite now runs through `benchmarks/run_paired_ab.py`, which
  alternates short baseline/current chunks (baseline chunk 0, current
  chunk 0, baseline chunk 1, ...) instead of two long runs, then merges
  each arm's chunks into one result JSON using the median of the per-chunk
  medians per scenario. This shrinks the wall-clock gap between a paired
  baseline/current measurement to roughly 1/chunks of the naive gap, which
  is what actually cancels the drift -- reordering the comparison math
  without shrinking that gap would not. The merged JSON has the same shape
  a plain `-m benchmarks.X` run always produced, so `ci_check_provenance.py`
  and `ci_compare.py` did not need to change.
- Every result JSON also records a fixed-workload CPU timing
  (`cpu_probe_seconds`, `benchmarks/_compat.py:cpu_probe`) and, once
  merged, the per-chunk series of that probe and of each chunk's wall
  time, so drift across the job is directly visible in the uploaded
  artifact instead of only inferable after a compare gate fails for no
  code-level reason.
- Validated by simulating a monotonic runner-speed drift over synthetic
  chunk data and running it through the real `ci_compare.compare()` and
  `run_paired_ab.merge_arm()`: at a drift rate calibrated to reproduce the
  magnitude of a real false-positive CI run, the old (unchunked)
  arrangement failed the 20% gate on a scenario with zero true
  regression, while 5-chunk interleaving brought the same synthetic
  scenario's measured delta down to low single digits -- and a genuine
  25%-slower regression injected into the same drifting model was still
  correctly caught (reported as ~31%, not cancelled) after chunking. A
  separate real run rebuilt a baseline venv and ran the orchestrator
  end-to-end to confirm the subprocess/merge mechanics, provenance, and
  dependency-version checks all still hold against real output.
