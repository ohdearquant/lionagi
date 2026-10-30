# Feature boundary inventory

`scripts/feature_boundaries.json` records one layer and feature owner for each
Python module under `lionagi/` and each third-party requirement in
`pyproject.toml`. A requirement can also name secondary users. Migration targets
are notes about destinations, not statements that a move has happened. The
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
names one exact source and target module, an issue number, an owner and a phase.
When a module or dependency is added or removed, update and review the checked
inventory; `scripts/build_feature_boundaries.py` can regenerate its candidate
entries from the ownership rules. Running the checker never infers ownership
for a missing entry.

The graph is built without importing the application. It includes static
imports, type-checking imports, import-error-guarded imports, supported lazy
export declarations, string annotations and literal dynamic imports. Computed
dynamic imports, aliases the scanner cannot resolve and native imports require
separate inspection. Internal names that could not be resolved are reported.

`tests/contracts/test_boundary_compatibility.py` records package declarations,
settings fields, extras and console-script wiring; the existing CLI surface
tests exercise selected command behavior. Update the fixture deliberately when
an intended compatibility surface changes.

`scripts/probe_import_effects.py` observes one listed module per fresh process.
It requires an independently enforced operating-system sandbox and scratch
`HOME` and `TMPDIR`; it does not establish that those controls are active.
Run it with `--help` for arguments. Its Python audit and state observations are
not exhaustive for native code, and an import interrupted by a denied effect
may conceal subsequent effects.
