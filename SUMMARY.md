# CI Gate Integrity — Summary

Branch: `show/lionagi-backlog/ci-gate-integrity` (off `origin/main`). Additive,
non-behavior-weakening changes only; no existing gate was removed or weakened.

## Per-issue root cause and fix

### #1776 / #1778 — `ci-gate` can pass while a real gating job failed or was skipped

**Root cause.** `.github/workflows/ci.yml` defined a path-filter job
(`changes`) whose output decides whether the Studio Docker build job
(`studio-docker`) runs. Two things made the aggregate (`ci-gate`) blind to a
failure in that chain:

1. `changes` was not in `ci-gate.needs`, so its `result` was never inspected
   by the aggregate at all — a failed `changes` run was invisible.
2. `ci-gate`'s check script read `join(needs.*.result, ' ')` and only failed
   on the literal substrings `failure` or `cancelled`. A `skipped` result —
   which is exactly what GitHub Actions reports for `studio-docker` when its
   `needs: changes` dependency fails (`studio-docker` has no `if: always()`
   override, so the implicit `success()` gate on `needs` suppresses it) — was
   treated as a pass.

Combined: if `changes` failed, `studio-docker` was marked `skipped` (not
`failure`), and `ci-gate` saw only `studio-docker=skipped`, which its
skip-tolerant check accepted as "an intentional no-op." A broken path-filter
job could silently disable the entire Docker gate and still show green.

**Fix.** `changes` was added to `ci-gate.needs`. The check script was
rewritten from a substring match to an explicit per-job comparison:
every hard gate (`lint`, `docs`, `test`, `frontend`, `studio-e2e`, `changes`,
`vscode`, `marketplace`) must report `result == 'success'`; anything else —
`skipped`, `failure`, `cancelled` — fails the aggregate. `studio-docker` gets
one named conditional exception: its `skipped` result is accepted only when
`changes` succeeded **and** reported no Studio-relevant path changed. In every
other case — `changes` failed/skipped/cancelled, or `changes` said inputs
changed but `studio-docker` did not succeed — `ci-gate` fails.

`studio-docker`'s own `if:` was also tightened, from
`if: needs.changes.outputs.studio == 'true'` to
`if: ${{ always() && needs.changes.result == 'success' && needs.changes.outputs.studio == 'true' }}`.
This makes the dependency intent explicit at the job definition itself; the
aggregate is still the authority that turns a non-`success` `changes` result
into a failed `ci-gate`, this just prevents `studio-docker` from evaluating
`outputs.studio` off a `changes` run that didn't actually succeed.

### #1777 — Studio Docker path filter narrower than the Dockerfile's real inputs

**Root cause.** The `changes` job's path filter watched `apps/studio/**`,
`lionagi/studio/**`, `pyproject.toml`, and the workflow file itself. The
Dockerfile's `COPY` instructions pull in `pyproject.toml`, `README.md`,
`LICENSE`, and the **full** `lionagi/` package (not just `lionagi/studio/`),
plus `marketplace/` and `.claude-plugin/` content. A change to, e.g.,
`lionagi/core/` or `README.md` could alter the built image without the
`changes` filter ever flagging it as Docker-relevant, so `studio-docker`
would be silently skipped for a PR that actually changes the image.

**Fix.** Broadened the filter to `apps/studio/**`, `lionagi/**` (full
package), `pyproject.toml`, `README.md`, `LICENSE`, `marketplace/**`,
`.claude-plugin/**`, and the workflow file. `uv.lock` was deliberately left
out: the Dockerfile installs via `pip install ".[studio]"` against
`pyproject.toml`, not `uv sync`, so the lock file is not a current build
input.

### #1796 — `--no-docker` removed without a deprecation path

**Root cause.** A prior CLI change replaced the old `--no-docker` /
`--web` / docker-by-default model with a mutually-exclusive mode group
(`--web`, `--docker`, `--no-frontend`, `--dev`), where Docker is now opt-in
via `--docker`. The old `--no-docker` argparse definition and its
consumption were both deleted with no compatibility shim, so any script or
habit still passing `--no-docker` now fails at argparse with "unrecognized
arguments" instead of getting the (now-default) non-Docker behavior it asked
for.

**Fix.** Restored `--no-docker` as a deprecated, hidden (`argparse.SUPPRESS`)
boolean flag on the studio parser (registered before the mode group, so it is
not part of the mutual-exclusion set). When present, `_studio_start` emits a
warning via the CLI's existing `warn()` logging channel
("`--no-docker` is deprecated and ignored; Docker is now opt-in with
`--docker`. Use bare `li studio` or `li studio --web` for the hosted UI.")
and otherwise proceeds exactly as it would without the flag — Docker was
already opt-in, so "ignore and warn" reproduces the old flag's practical
effect (no Docker) without resurrecting the old Docker-by-default branch.
Two tests were added covering `li studio --no-docker` and
`li studio --no-docker start`, asserting the process still starts (return
code 0, hosted mode) and the deprecation warning appears on stderr.

## Why the aggregate now fails when a gating job fails — the result-propagation argument

The failure mode in all three workflow issues above is the same shape: a
job's `result` can be something other than `success` (`failure`, `skipped`,
`cancelled`) without the aggregate treating that as disqualifying, because
either (a) the job wasn't in `ci-gate.needs` at all, or (b) the aggregate's
check accepted `skipped` unconditionally.

The new `ci-gate` check step reads `needs` as a JSON object
(`toJSON(needs)`, GitHub's structured context, not a joined string) and for
every name in the explicit `hard_gates` list asserts
`needs[name]["result"] == "success"`. There is no wildcard, substring match,
or default-to-pass branch — a name either reports `success` or it is
collected into the failure list. `studio-docker` is deliberately excluded
from `hard_gates` and handled by its own branch, because it is the one job
whose `skipped` result can be legitimate.

Paper cases (cannot run the full GitHub matrix locally; reasoned from the
`needs` context contract GitHub documents — each job's `result` is one of
`success`, `failure`, `cancelled`, `skipped`):

| `changes` result | `changes.outputs.studio` | `studio-docker` result | `ci-gate` outcome | Why |
|---|---|---|---|---|
| `failure` | (job didn't complete, no output) | `skipped` (implicit `success()` gate suppresses it) | **fails** | `changes != success` is in `hard_gates`; the substring/skip-tolerant path is gone. |
| `cancelled` | n/a | `skipped` | **fails** | Same: `changes != success`. |
| `skipped` (e.g. upstream anomaly) | n/a | `skipped` | **fails** | Same: `changes != success`. |
| `success` | `false` | `skipped` | **passes** | `changes == success` and the `studio-docker` branch takes the `studio_changed != "true"` arm, which accepts `skipped` — the intentional unrelated-PR path. |
| `success` | `true` | `skipped` / `failure` / `cancelled` | **fails** | `studio_changed == "true"` requires `studio_result == "success"`; anything else is appended to `bad`. |
| `success` | `true` | `success` | **passes** | Both branches satisfied. |
| any non-conditional hard gate (`lint`, `docs`, `test`, `frontend`, `studio-e2e`, `vscode`, `marketplace`) reports `skipped`/`failure`/`cancelled` | — | — | **fails** | Same explicit per-job check; there is no gate left that tolerates a non-`success` hard gate. |

The key propagation fact this relies on (GitHub Actions' documented `needs`
context behavior, not an assumption): a job's `result` in the `needs`
context is always one of the four literal values above, and a job that never
ran because its own `needs` failed reports `skipped`, not `failure` —
that is precisely why a naive `if: success()` / substring check on the
aggregate side is not sufficient, and why the fix has to compare each name
individually against the literal string `"success"` rather than trying to
exclude `failure`/`cancelled` and implicitly trust everything else.

## Local validation run

- `python -c 'import yaml, pathlib; yaml.safe_load(pathlib.Path(".github/workflows/ci.yml").read_text())'` — YAML parses successfully after all `ci.yml` edits (run via `uv run python -c ...`).
- `uv run --extra studio pytest tests/cli/test_studio_cli.py` — all 38 tests pass, including the 2 new `--no-docker` deprecation tests.
- `uv run ruff check lionagi/studio/cli.py tests/cli/test_studio_cli.py` — no findings.
- The full GitHub Actions matrix cannot be run locally; correctness of the
  `ci-gate` rewrite rests on the paper-case table above, derived from
  GitHub's documented `needs` context contract.

## Files changed

- `.github/workflows/ci.yml` — fail-closed `ci-gate` aggregation; explicit
  `studio-docker` conditional; broadened Docker path filter.
- `lionagi/studio/cli.py` — deprecated `--no-docker` warn-and-ignore flag.
- `tests/cli/test_studio_cli.py` — two new tests for the deprecation path.
- `docs/adrs/ADR-0097-ci-gate-taxonomy.md` — gate taxonomy, concrete gap
  list, and founder-gated proposed changes kept separate from what is
  implemented here.

## Proposed but not implemented (founder sign-off required)

See `docs/adrs/ADR-0097-ci-gate-taxonomy.md` for the full table: a PR-time
test leg for the Python version the Docker runtime uses, converting the
type-check job from advisory to blocking, folding security scanning into the
aggregate, and defining performance-regression thresholds before any
perf gate becomes merge-blocking. None of these are enforced by this change.
