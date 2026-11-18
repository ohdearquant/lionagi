# Feature boundary inventory

`scripts/feature_boundaries.json` records one layer and feature owner for each
Python module under `lionagi/` and each third-party requirement in
`pyproject.toml`. A requirement can also name secondary users. Migration targets
are notes about destinations, not statements that a move has happened. A target
that depends on a pending decision names the decision instead of a destination. The
inventory does not assign owners inside `lionhub`; its modules are graph
targets at the downstream composition-root layer.

Run the static, report-only check from the repository root:

```sh
python scripts/check_feature_boundaries.py
python scripts/check_feature_boundaries.py --json > feature-graph.json
```

The check fails for malformed data, missing or stale module and requirement
entries, or invalid exceptions. It reports upward imports, sibling-adapter
imports and cross-root imports without failing on existing debt. An exception
names one exact source and target module, an issue number, an owner and a removal
phase spelled `P1` through `P5`. The pair must still be a boundary finding.
When a module or dependency is added or removed, update and review the checked
inventory; `scripts/build_feature_boundaries.py` can regenerate its candidate
entries from the ownership rules. Running the checker never infers ownership
for a missing entry.

The graph is built without importing the application. It includes static
imports, type-checking imports, import-error-guarded imports, supported lazy
export declarations, string annotations and literal dynamic imports. Computed
dynamic imports, aliases the scanner cannot resolve and native imports require
separate inspection. Internal names that could not be resolved are reported.
An alias rebound in one file resolves to its last binding within that scope.

An internal import that resolves through the literal `MOVED` table appears in
`resolved_aliases` with its original edge, canonical module and target owner.
Physical modules take precedence. The longest matching alias prefix selects
the canonical destination, and any missing destination stays unresolved.
The edge list retains the source spelling. Boundary findings classify both
direct and aliased imports by their canonical destination. An alias finding
carries its original name in `legacy_target`. The nine root LNDL exports carry
a `grandfathered` disposition with ADR-0136 Amendment 1 A3 as the basis.
A Core destination without a manifest owner fails the check.

`tests/contracts/test_boundary_compatibility.py` records package declarations,
public definitions, moved-module mappings, settings fields, direct environment
lookups, extras and console-script wiring; the existing CLI surface tests
exercise selected command behavior. Update the fixture deliberately when an
intended compatibility surface changes. Computed environment names and dynamic
exports still need their own behavior tests.

`scripts/probe_import_effects.py` observes one listed module per fresh process.
It requires an independently enforced operating-system sandbox and scratch
`HOME` and `TMPDIR`; it does not establish that those controls are active.
Run it with `--help` for arguments. Its Python audit and state observations are
not exhaustive for native code, and an import interrupted by a denied effect
may conceal subsequent effects. Exit status 0 means the import completed, 1
means it raised, and 2 indicates an invalid invocation or account boundary.

## Default-runtime import-effects baseline

The baseline used revision `02d867120fb9de7bab09b2f953259defab879c04`
and CPython 3.14.7 on Darwin arm64. The repository's `.python-version`
selects 3.10. The baseline staged 31 third-party distributions
from the lock file and loaded LionAGI from the source checkout. The
interpreter was staged outside the operating-system account's home
directory. Each tracked production module under `lionagi/` and
`lionhub/src/lionhub/` was attempted in a fresh interpreter started with
`-B`, which disabled bytecode cache writes.

The pinned `scripts/probe_import_effects.py` has SHA-256
`d609ddce18d66e5f65cbb6515df3874db3197d344d4f855b7a939a4e86b8e479`.

A `PermissionError` at CPython's `os.uname()` call cut short 72 imports.
Another 43 ended with 40 missing-module errors and three optional-feature
import errors. Effect counts for both groups cover only the import steps
before the reported error. The paired control covered two modules, and both
imports completed when platform identification was available.

| Source | Outcome | Imports | Filesystem reads | Logger mutations | Dropped reads |
| --- | --- | ---: | ---: | ---: | ---: |
| Core | Completed | 272 | 87,133 | 537 | 43,116 |
| LionHub | Completed | 300 | 80,880 | 595 | 36,496 |
| Both | Completed | 572 | 168,013 | 1,132 | 79,612 |
| Core | Cut short at `os.uname()` | 37 | 12,159 | 42 | 6,269 |
| LionHub | Cut short at `os.uname()` | 35 | 13,009 | 92 | 7,640 |
| Both | Cut short at `os.uname()` | 72 | 25,168 | 134 | 13,909 |
| Core | Other import errors | 25 | 3,507 | 30 | 783 |
| LionHub | Other import errors | 18 | 1,526 | 3 | 633 |
| Both | Other import errors | 43 | 5,033 | 33 | 1,416 |
| Both | All attempts | 687 | 198,214 | 1,299 | 94,937 |

The probe counted the following attempts before applying its event retention
limits. It recorded zero retained events marked as blocked by the probe in
either source set. The probe records operating-system containment as a caller
assertion.

| Probe-recorded attempt | Core | LionHub |
| --- | ---: | ---: |
| Filesystem write (`filesystem.write`) | 0 | 0 |
| Filesystem read and write (`filesystem.read_write`) | 0 | 0 |
| Directory change | 0 | 0 |
| Account-home access | 0 | 0 |
| Network operation | 0 | 0 |
| SQLite connection | 0 | 0 |
| Subprocess or process creation | 0 | 0 |
| Background thread start | 0 | 0 |
| Background task creation | 0 | 0 |

Across all attempts, the before-and-after snapshots showed differences in
logger state for 627 attempts, selected registries for 277, and the import
finder list for 624. They showed zero persistent differences in the
environment, `sys.path`, or the working directory. Individual environment reads
fall outside the snapshots. The audit covers instrumented Python events during
module import in the baseline dependency profile. Native activity outside those
channels, feature activation and other dependency profiles were outside the
measurement.

For another capture, pin the source, interpreter and dependency profile.
Build separate sorted UTF-8 lists for Core and LionHub from the tracked
Python files. Use repository-relative paths for Core and strip the
`lionhub/src/` import root for LionHub before converting the paths into dotted
module names. Use each package initializer's package name. Write one name
per line with a final newline.
Compare the regenerated baseline module lists with these SHA-256 hashes.

| Source | Module-list SHA-256 |
| --- | --- |
| Core | `0d2d7066584431eb0c73fcfb3f33e2a05b13ca4cc4a72e36b409c38c28590e70` |
| LionHub | `87a0141d42c20454ff045b7a46294eb98052084d2d40b173032a668cd20b4ac8` |

Dropped read records limit individual inspection. The aggregate read totals
include reads counted before the filesystem-read retention cap.
