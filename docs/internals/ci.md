# CI reliability

`scripts/ci.sh` is the single entry point for Python checks in CI, local
development, and pre-commit. Workflows select a command and set policy through
environment variables; they do not inline pytest commands.

## Release images

The Release workflow resolves one published `v<version>` release to a full
commit SHA. Its test matrix, wheel publisher and container build all check out
that SHA. The tag must match the source package's version. A new release event
also requires the tag to match the event's commit; it can publish a wheel that
does not exist on PyPI yet. For release events, the image job waits for the
wheel publisher to finish, then checks PyPI for a non-yanked wheel. A failed
publisher result does not skip that check: an upload can reach PyPI even when
the client reports failure or a retry reports an existing file. The source and
test jobs must still succeed. A missing or yanked wheel prevents image publication.
A failed upload job still leaves the overall workflow failed even if its image
is subsequently published.

To repair a missing container image, dispatch `release.yml` **from the default
branch**, with the published version as its `version` input, without `v`. The
current workflow helpers run from the dispatch commit, while the build context
comes from the released commit. A repair does not run the wheel publisher. It
also retains that release's Dockerfile; package, frontend or Dockerfile changes
require a new release rather than republishing changed source under an old tag.
The rebuild still resolves its base images, apt packages and Python dependencies
on the dispatch day, so a repaired image matches the released one in the source
it builds from, not byte for byte. The image job waits on the test job, so a
repair also reruns the released commit's test matrix and a red test there blocks
the image repair.
The manual repair requires all of:

- A published, non-draft GitHub release with the exact version tag.
- A matching package version in the released `pyproject.toml`.
- A non-yanked wheel for that exact version on PyPI.
- A successful `deploy` job and `Publish package to PyPI` step in a release-event
  run of `release.yml` at the same tag and commit. Earlier successful attempts
  count when a later retry fails because the wheel was already uploaded.

The last check uses retained Actions history as evidence that the repository's
release workflow published this commit: it reads the run's workflow path, job
name and step name, and it does not compare that run's workflow file against the
default branch. It is not a cryptographic attestation of wheel contents: it
relies on that workflow testing and uploading its run's source. Missing or
expired publication history refuses a manual repair; it does not prevent a new
release event from publishing normally. A release whose wheel never reached PyPI
is the other case: rerun its release-event run so the deploy job retries the
wheel upload, and the image job runs after it. A moved tag without matching
publication evidence is refused. If an upload reached PyPI
but no attempt recorded a successful upload, a default-branch repair is refused
with `no successful release workflow wheel upload binds this tag to its commit`.
This refusal depends on the missing upload record alone, so it applies whether or
not that release's image job also failed, for example during Buildx setup.
Dispatching a repair cannot recover this case. For release-event
runs that already include this workflow version, rerun the original release-event
workflow to retry its image job while GitHub still permits a rerun. That path
compares the tag with the recorded event commit and source package version, then
checks the actual PyPI wheel, without requiring a successful upload-step
conclusion. If wheel upload is retried after PyPI accepted it, the duplicate
upload can still fail and leave the run red even when the image publishes.
An older run does not gain this recovery behavior by rerunning:
[reruns retain their original workflow and commit](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)
and are subject to GitHub's rerun limits.

The default-branch dispatch check is a process guard inside the workflow and
helper code. With that code unchanged, a branch dispatch is rejected before
tests, and the image is built from the release commit. A maintainer able to
change the workflow or helper can remove the guard; this is not an external
default-branch deployment restriction. No GitHub environment rule is assumed.
The guard constrains only the copy of the workflow it runs in: a principal with
branch-push and workflow-dispatch rights can run a branch copy of `release.yml`
that publishes arbitrary content under an existing version tag and `latest`, and
that copy's jobs can still reach the repository secrets the release path uses.
The guard binds the published source to the released commit only for the
workflow on the default branch.
Both paths trust this repository's publication workflow and do not prove that
PyPI wheel bytes equal the checked-out source.

The docker job uses the shared `lion-studio-image` concurrency group with
`cancel-in-progress: false` and a 60-minute timeout. The source/PyPI revalidation,
image push, and registry verification occupy one slot, so an older repair cannot
make its latest-tag decision before a newer image job and then overwrite that
job's push. With this default concurrency
configuration, GitHub keeps at most one pending job in the group; a newly
queued job can cancel and replace an older pending job even though
`cancel-in-progress` is false. The displaced release can therefore finish red
without publishing an image. The daily `Image matches PyPI` workflow detects a
missing current-version image or stale `latest`. Rerun the displaced release-event
run (Actions, then the run, then `Re-run all jobs`) to recover its image while
reruns remain available. That control also reruns the run's `deploy` job, which
re-attempts `twine upload`; when PyPI already has the wheel the duplicate upload
can fail and leave the run red even though the image publishes (see the
duplicate-upload caveat above). If a manual repair was displaced, dispatch it
again from the default branch when its evidence is retained.
Avoid overlapping recovery dispatches so they do not replace another pending job.
See
[GitHub's concurrency behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
The daily check compares the version PyPI currently serves; it does not detect
every missing older-version image, so check a displaced older release separately.

Inside that slot, the workflow rechecks the source binding and PyPI version
before building. It publishes the version tag, and updates `latest` only when
PyPI reports that version as its current default at revalidation. The push
replaces that version tag if it already exists; nothing reads the registry first,
so treat the dispatch as a rebuild for a version, not only as a repair, and
expect the tag's digest to change. PyPI's version index can lag a fresh upload,
so a release whose own version is not yet the version PyPI reports as current
leaves `latest` untouched on that run; the daily `Image matches PyPI` check names
`latest` as stale, and a later image run whose revalidation reads the advanced
version moves it. Nothing schedules that run: it is the next release's image
job or a manual default-branch dispatch for that version. Older versions and
prereleases that PyPI does not select therefore leave `latest` alone. This
serializes these image jobs, not external publishers or wheel publication; PyPI
can advance while an image builds, before its newer image job runs.

The image carries its version and source SHA as OCI labels. Before releasing
the concurrency slot, verification compares registry manifest digests with the
digest returned by this run's image build, including `latest` when published.
A pre-existing version tag cannot make a failed build pass. The separate
`verify-image` job rejects skipped, cancelled or failed docker jobs, and requires
successful digest verification and a valid build digest. The in-slot registry
verifier runs only after the image build was attempted, preserving an earlier
source or wheel refusal as the cause of failure. The final gate does not reread
tags after releasing the slot. A later image publication cannot invalidate an earlier
run's completed registry check.

`tests/scripts/test_release_image.py` exercises the production resolver with
mock GitHub/PyPI metadata, verifies workflow source wiring, and runs the registry
verification shell with mock HTTP responses. These checks do not publish images.

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
commands retain the faster defaults of 3 and 1. The Python runner (`scripts/ci.sh`)
defaults `MAX_WORKER_RESTART` to 4 in all three test commands; CI does not override it.
Worker crashes still identify the owning test. The JUnit census rejects missing
collected test IDs after an early stop, and the test-step and job timeouts in
`.github/workflows/ci.yml` bound a controller stalled on worker replacement.

`performance` tests run serially in `ci.yml`'s advisory `performance` job,
outside the required matrix. Tests marked `slow_timing` exercise real deadline
behavior and must have at least 10x scheduling margin. There are no blanket
test retries.

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

`LIONAGI_STATE_DB_URL` is removed from the environment at the same point, in
every spelling: the settings loader matches environment names without regard to
case. It names the state store directly and outranks `LIONAGI_HOME`, so a run
from a shell that exports it would open that shell's store however
`LIONAGI_HOME` was redirected. Tests that need the setting set it themselves.
Settings are read once, when `lionagi.config` is first imported, so the conftest
refuses to start if that module is already loaded (a plugin imported it first)
rather than run with a store URL it could no longer remove. The settings loader
also reads `.env`, `.env.local` and `.secrets.env` from the directory the suite
runs from, and `.env` under `LIONAGI_HOME`, which the redirect above points at the
test home; the repo keeps none of them, and no test module loads a `.env` of its own
except `tests/test_home_dotenv.py`, which writes the test home's `.env`, removes it
afterwards, and skips rather than replace one a `LIONAGI_TEST_HOME` already holds. `test_an_exported_store_url_reaches_neither_the_suite_nor_its_children`
checks the removal from outside, opening the default store in a child process
for each spelling, and
`test_a_suite_whose_settings_were_read_before_the_removal_refuses_to_run`
checks the refusal.

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
