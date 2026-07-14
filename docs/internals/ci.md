# CI reliability

`scripts/ci.sh` is the single entry point for Python checks in CI, local
development, and pre-commit. Workflows select a command and set policy through
environment variables; they do not inline pytest commands.

## Test lanes

The required `test` matrix runs Python 3.10 and 3.14 on pull requests and the
full 3.10-3.14 matrix on protected-branch pushes. Python 3.10 carries coverage.
The required marker expression is:

```text
not performance and not flaky_quarantine
```

CI runs every failure on non-coverage legs (`MAXFAIL=0`). The coverage leg uses
a catastrophic-failure ceiling of 25 because it is the critical path. Local
commands retain the faster defaults of 3 and 1. Worker restarts remain disabled
so a hard crash names the test that owned the worker.

`performance` tests stay in the benchmark workflow. Tests marked `slow_timing`
exercise real deadline behavior and must have at least 10x scheduling margin.
There are no blanket test retries.

The separate `quarantine` job runs only `flaky_quarantine` tests on Python 3.12.
It is `continue-on-error` and intentionally absent from `ci-gate`; its result is
visible but cannot block a merge. `ci-gate` remains fail-closed for every
existing hard gate, with Studio Docker as its sole path-filtered exception.

## Quarantine lifecycle

`tests/quarantine.txt` is the source of truth. Each non-comment line is:

```text
YYYY-MM-DD | exact pytest nodeid | first assertion or exception signature
```

Collection applies the marker from the manifest, so source tests do not carry
stale quarantine annotations. The lint job validates the manifest and fails
above 15 entries, naming the oldest entries.

A test enters only after two independent false failures with the same signature.
Before adding it, retain the failure artifacts and confirm the failures came
from separate run IDs. Quarantine is containment, not a substitute for a root
cause investigation.

A test exits after its root-cause fix lands and its quarantine step is green for
50 consecutive workflow runs. In the Actions history, inspect the `Run
quarantined tests only` step rather than the overall continue-on-error job. This
command lists the run IDs used for that review:

```bash
gh run list --workflow ci.yml --limit 50 --json databaseId,createdAt,headSha
```

Use `gh run view RUN_ID --json jobs` to verify that step's conclusion for every
run. Remove the manifest line only after all 50 are `success`; the next required
run proves the test has rejoined the blocking lane.

## Failure telemetry

Every failed test leg uploads `failures.jsonl`. Each record contains the exact
nodeid, Python matrix leg, first failure signature, run ID, and attempt. Generate
a windowed report from downloaded artifact directories:

```bash
uv run python scripts/flake_report.py path/to/downloaded-artifacts/
```

Or let the report download matching artifacts from run-list JSON:

```bash
gh run list --workflow ci.yml --limit 60 --json databaseId \
  | uv run python scripts/flake_report.py --gh-runs -
```

The report separates failure occurrences from distinct run IDs, lists every
signature with its own run count, and labels each nodeid as quarantined or new.
The peak-RSS report remains the companion diagnostic for hard worker crashes.

## Timeouts and distribution

The per-test timeout is 300 seconds. Pytest's faulthandler dumps every thread at
270 seconds, before pytest-timeout's thread-method termination. Lowering the
test timeout requires a full non-performance duration run and at least 3x
headroom over its slowest legitimate test.

The required lane retains `--dist loadfile`. Some test files mutate process
environment and module state, so file affinity is part of isolation. Duration
data should be reviewed for stragglers before proposing `loadgroup`; a change
also requires explicit `xdist_group` markers for every state-leaking file.
