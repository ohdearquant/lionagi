# Public-Surface Breaking-Change and Deprecation Policy

**Purpose**: Define what counts as public surface in LionAGI, require an explicit deprecation
path before removing or renaming anything on that surface, and establish CHANGELOG discipline
for communicating changes to consumers.

---

## 0. Scope while this repository is own-use

Under the owner directive of 2026-07-11 declaring this repository own-use — built for our own
consumption, not constrained by external callers, with redirect/compatibility shims named as the
thing not to do — the alias-plus-warning-plus-release cycle in section 2 does not bind this
repository while that directive stands. It continues to bind packages this repository ships to
external callers. Two things do not change under this scope clause: every public removal still
requires a `Removed` CHANGELOG entry (the audit record was never in tension with own-use), and
reviewers reject a public removal that lacks one, regardless of whether the deprecation cycle
applied.

---

## 1. What Counts as Public Surface

The following are public surface. Removing, renaming, or incompatibly changing them requires
the deprecation path in section 2.

- **Top-level `lionagi` exports**: every name reachable via `from lionagi import <name>` or
  listed in `lionagi/__init__.py`'s `__all__`.
- **`lionagi.ln` and `lionagi.state` exports**: everything in their `__all__`.
- **Any package `__all__`**: a name in `__all__` is a public commitment, regardless of depth.
- **Documented hook names** (`HookPoint` enum values and string keys referenced in docs or
  `AgentSpec` / `HooksMixin` public APIs).
- **CLI flags and subcommands**: flags of `li`, `li agent`, `li o flow`, `li o fanout`,
  `li schedule`, and `li studio` that appear in `--help` output.
- **Provider and endpoint names**: string identifiers accepted by `iModel` / `match_endpoint`
  that are documented or emitted in error messages.

Names prefixed with `_`, not listed in `__all__`, and not referenced in official docs are
internal. Internal names may change without notice.

---

## 2. The Deprecation Path

Before removing or renaming a public name, follow all four steps.

**Step 1: Keep a backward-compatible alias and emit a warning.**

```python
import warnings

# Old name stays; new name is the canonical one.
def new_name(*args, **kwargs): ...

def old_name(*args, **kwargs):
    warnings.warn(
        "old_name is deprecated and will be removed in a future minor release. "
        "Use new_name instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return new_name(*args, **kwargs)
```

**Step 2: Add a CHANGELOG "Deprecated" entry** under `[Unreleased]` (see section 4).

**Step 3: Wait at least one minor release.** The alias and warning must ship together in a
released version before the old name can be removed.

**Step 4: Remove the alias in a later minor or major release.** Add a "Removed" entry to
CHANGELOG. Do not remove silently.

There are no exceptions to silent removal. If a public name must disappear immediately due to
a security defect or a name that was never functional (see section 3), document why in the
commit message and CHANGELOG.

---

## 3. Allowed Without Deprecation

These changes may be made without the alias-plus-warning cycle.

- **Purely additive exports**: adding a new name to `__all__` or a new CLI flag. Existing
  consumers are unaffected.
- **Fixing a name that never worked**: if importing or calling the name has always raised an
  exception (e.g., missing dependency, broken wiring), it was never a functional public
  contract. Document this in the commit message.
- **Internal-only changes**: refactoring or renaming anything not in `__all__` and not
  documented. Confirm the name does not appear in any example, cookbook, or docs page first.
- **Type annotation narrowing that is source-compatible**: adding `| None` or a more specific
  return type does not break callers.

---

## 4. CHANGELOG Discipline

Maintain `CHANGELOG.md` using Keep-a-Changelog conventions. Each release version has up to
five sections. Only include sections that have entries.

```markdown
## [Unreleased]

### Added
- Short description of new capability.

### Changed
- Short description of a non-breaking behavioral change.

### Deprecated
- `old_name` in `lionagi.ln` — use `new_name` instead. Will be removed in a future release.

### Removed
- `old_name`, deprecated since v0.X. Use `new_name`.

### Fixed
- Short description of a bug fix.
```

Every PR that touches public surface must update `[Unreleased]`. Code reviewers reject a patch
that removes or renames a public name and has no `Removed` (or `Deprecated`, where section 2
still applies) CHANGELOG entry for it; a removal with the required entry present does not trigger
this instruction, deprecation-cycle scope under section 0 notwithstanding.

---

## 5. Mechanism

Use Python's stdlib `warnings` module with `DeprecationWarning`. No new framework is needed.
Set `stacklevel=2` so the warning points to the caller's line, not the alias itself.

`DeprecationWarning` is ignored by default in non-`__main__` contexts. Consumers who want
to see them run with `-W default::DeprecationWarning` or use `pytest`'s `-W` flag. This is
intentional: it surfaces warnings in test suites without spamming production logs.

Do not use `FutureWarning` (reserved for user-facing warnings from end-user scripts) or
`PendingDeprecationWarning` (too quiet). `DeprecationWarning` is the correct choice.
