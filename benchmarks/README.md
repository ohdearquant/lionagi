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
