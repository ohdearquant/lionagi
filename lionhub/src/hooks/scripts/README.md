# lionhub.hooks.scripts

Small, standalone hook programs invoked from declarative hook configuration. Each reads one JSON object on standard input, inspects the proposed action or result, and signals its verdict through the exit status: zero allows, two refuses with a reason on standard output. It is not the hook bus or the external-hook adapter; it contains no policy engine.

Final import path: `lionhub.hooks.scripts` (ADR-0136 D7). The reference configuration launches these modules by name under the active interpreter.

| Slot | What it holds | Where the code comes from |
|---|---|---|
| `__init__.py` | Package marker and shared invocation convention. | `lion1/hub/hooks/scripts/`; the three programs below are the concept source. |
| `guard_bash.py` | Refuses destructive shell commands named by its pattern list. | `lion1/hub/hooks/scripts/guard_bash.py`. |
| `no_secrets.py` | Refuses reads or searches that would expose a secret file. | `lion1/hub/hooks/scripts/no_secrets.py`. |
| `tree_changed.py` | Reports whether the working tree changed during an operation. | `lion1/hub/hooks/scripts/tree_changed.py`. |

The draft has no separate slot for the reference configuration; it lives in `hooks.toml` one level up.

## Who uses it

- The reference hook configuration in the parent folder: its rows launch `python -m lionhub.hooks.scripts.<name>`.
- User and project hook configuration loaded by the external-hook executor in `lionhub.hooks`: may launch the same modules.
- The standalone command-line entry point is preserved for each program, so configuration does not need an import.

## Status

New code, ported from the reference hook programs as its own change with its own tests. It is not a move from `lionagi`, so it adds no row to the old-to-new import table.
