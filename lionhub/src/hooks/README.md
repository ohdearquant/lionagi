# lionhub.hooks

The external-hook executor: it runs a configured command as a real subprocess, passes a JSON event envelope on standard input, and turns the exit status and output into a decision. It supports session and tool events and the trust checks that precede execution. It is not the session event bus: the bus, its default persistence handlers and the retry queue remain in the core.

Final import path: `lionhub.hooks` (ADR-0136 D7). The moved `lionagi.hooks.external` path keeps working through 1.x as the same module object, with a `DeprecationWarning` naming the new path; 2.0 removes it. The core's `lionagi.hooks` initializer does not import the executor, so `import lionagi` loads nothing from this package.

| Slot | What it holds | Where the code comes from |
|---|---|---|
| `__init__.py` | Package surface for the external-hook adapter and its event vocabulary. | `lionagi/hooks/external.py`; the core initializer stays in the core. |
| `gate.py` | Gate and decision contract used by blocking hook events. | `lionagi/protocols/action/gate.py`; `lionagi/agent/gate.py` is the compatibility export. |
| `guard.py` | Built-in destructive-command and path guards exposed through the package. | `lionagi/agent/hooks.py`; the standalone guard program is a concept from `lion1/hub/hooks/scripts/guard_bash.py`. |
| `hooks.toml` | Reference declarative hook configuration. | `lion1/hub/hooks/hooks.toml`. |
| `scripts/` | Small executable guards launched from configuration. | `lion1/hub/hooks/scripts/`. |

The event bus and its default handlers (`bus.py`, `loader.py`, `persist.py`, `builtins.py`, `_message_retry.py`) are not in this draft and do not move here. The package initializer must not import them; the core keeps building a session's bus on first use.

## Who uses it

- `lionagi/agent/factory.py` and `lionagi/agent/settings.py`: construct external hook adapters for an agent.
- `lionagi/session/branch.py`: registers external hooks for a branch when a bus is present.
- `lionagi/cli/hooks.py`: imports the adapter, event set and trust helpers for the `li hooks` commands.
- `lionagi/cli/_runs.py`: routes message persistence and retry handling, which remain core hooks.

## Status

Moves next in wave 3, after `agent`, as `lionhub.hooks.external`. The bus and persistence modules stay in the core under their current paths and add no row to the old-to-new table.
