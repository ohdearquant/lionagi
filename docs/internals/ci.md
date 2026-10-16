# CI reliability

`scripts/ci.sh` is the single entry point for Python checks in CI, local
development, and pre-commit. Workflows select a command and set policy through
environment variables; they do not inline pytest commands.

## Test lanes

The required `test` matrix runs the full Python 3.10-3.14 set on pull requests
and on protected-branch pushes; a manual run can ask for 3.10 and 3.14 only.
Python 3.10 carries coverage on pushes and manual runs, never on pull requests.
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
visible but cannot block a merge. `performance` and `publish` are not among
`ci-gate`'s needs either. `ci-gate` is fail-closed for every job it needs: each
hard gate must succeed, and the five path-filtered gates (Studio Docker, minimal
import, no eager heavies, lionrs and lion1) may skip only when the `changes` job
succeeded and reported their inputs unchanged.

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

## Run directory isolation

`lionagi._paths` reads `LIONAGI_HOME` once, at import, and derives `RUNS_ROOT`
from it; several modules then bind those two constants into their own
namespace by name. `tests/conftest.py` redirects `LIONAGI_HOME` to a fresh
temporary directory before anything else in the suite can import `lionagi`,
so that every test's run-directory writes (manifests, branch snapshots,
stream buffers) land in a throwaway location instead of interleaving with a
developer's real store or a persistent CI one. The redirect has to sit above
every other import in `conftest.py`, because once `lionagi._paths` is
imported the constants it derived are fixed for the process.

The value is overwritten unconditionally rather than only filled in when
absent. `LIONAGI_HOME` is an ordinary production variable that plenty of
shells and CI environments already set for reasons unrelated to the suite;
deferring to an existing value would let that ambient setting silently turn
isolation off. `LIONAGI_TEST_HOME` is the deliberate way through this: when
set, the suite adopts it as `LIONAGI_HOME` and writes there instead of a
throwaway directory. Anything written under it survives the run — it is not
cleaned up — so it exists for a case that actually wants the suite pointed at
a specific, inspectable directory. When neither is a concern, the suite
allocates a temp directory and registers its removal with `atexit`.

`HOME` is replaced the same way, unconditionally and before any import, with a
temporary directory of its own. Discovery reads the user-level `.lionagi`
(agent profiles, MCP configuration, `settings.yaml`) through `HOME` on every
call, so a run that kept the developer's `HOME` would hand any test that builds
a profiled agent the real directory. Child processes the suite starts inherit
the replacement. When the password database identifies the account home,
project walks exclude its `.lionagi` regardless of `HOME` (ADR-0088 Amendment 1),
and spawned MCP discovery skips a bare `.mcp.json` there. If the account home
cannot be identified, that exclusion is unavailable. Together with the `HOME`
replacement, these checks keep tests from loading account-level configuration
from a checkout below the account home.
`test_the_callers_home_reaches_neither_the_suite_nor_its_children` in
`tests/test_run_directory_isolation.py` checks the replacement from outside.

Cleanup runs after pytest has already reported the session's result, so a
failure there has nowhere left to be an assertion. `_remove_test_home`
therefore never raises: it catches `OSError` and `RecursionError` (the latter
because `shutil.rmtree` descends recursively and a sufficiently deep tree
exhausts the stack without the filesystem objecting to anything) and writes
the surviving root and the underlying error to stderr instead. Nothing wider
is caught — a bug inside the cleanup call itself (a `TypeError` from a
misused API, say) is left to propagate rather than being reported as a
directory the machine refused to delete.

`tests/test_run_directory_isolation.py` verifies this from the outside, since
by the time a test is running inside the suite the redirect has already
happened and the original environment is gone. It launches a second pytest
process with `LIONAGI_HOME` pre-set to a disposable directory (through a
`probe` fixture that writes a throwaway test file under a dot-directory in
`tests/`, so real `conftest.py` discovery still applies to it, and runs it
with `-n0` to avoid xdist startup overhead for a single test) and reads back
what the child process actually bound `_paths.LIONAGI_HOME` and
`_paths.RUNS_ROOT` to. That is also how the suite verifies its own
`atexit` cleanup: only a second process can observe what a first one wrote to
stderr on the way out.

One probe simulates a root that cannot be removed at all, using file
permissions: `chmod(0o500)` on a subdirectory makes it impossible for the
owning process to unlink the entry inside, so `rmtree` walks in and stops.
The probe records the paths it is about to lock *before* locking them, so a
caller can find and clear them even if the probe process hangs or is killed
before it can report anything else. Recovery (`_recover_reported_roots`)
processes every recorded root even after one fails: it collects failures
and raises them together at the end rather than stopping at the first one,
because a loop that stops early would strand every later root with nothing
left to unlock it. The per-record exception handling is deliberately broad —
everything caught is named in the consolidated failure — so an unrelated
error can't decide silently that the remaining roots aren't worth
attempting.
