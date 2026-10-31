# lionhub.agent

The layer that constructs and configures a running agent: a portable specification, the factory that turns it into a branch with a model, tools and guards, permission policy, roles, and communication between agents. It is not the branch/session runtime itself, which stays in the core; nor the governance engine in `lionhub.canon`; nor a command-line front end.

Final import path: `lionhub.agent` (ADR-0136 D7). The moved `lionagi.agent.*` paths keep working through 1.x as the same module objects, with a `DeprecationWarning` naming the new path; 2.0 removes them. A plain `import lionagi` loads nothing from this package.

| Slot | What it holds | Where the code comes from |
|---|---|---|
| `__init__.py` | Package surface: `AgentSpec`, `create_agent`, permission policy and settings helpers. | `lionagi/agent/__init__.py`; the rest of `lionagi/agent/` moves beside it. |
| `cast.py` | Roles and modes as composable, frozen configuration atoms, plus the read-only catalog of built-in roles and modes. | `lionagi/casts/pattern.py`, `lionagi/casts/catalog.py` and `lionagi/casts/roles/`. |
| `role.py` | The role type and its lookup path for the agent layer. | `lionagi/casts/pattern.py`; built-in role modules under `lionagi/casts/roles/` supply the instances. |
| `seat.py` | Reserved slot for a named agent identity record. | new; no existing code. It is intended to give an agent a stable, addressable identity separate from its role. |
| `communication.py` | Message construction and delivery between agents and teams. | `lionagi/tools/communication/messenger.py` and `lionagi/cli/team.py`. |
| `inbox.py` | Durable per-agent message collection and retrieval. | new; no existing code. It is intended to hold queued messages for an agent independently of a conversation branch. |

The factory and its supporting modules move with the package: `factory.py`, `spec.py`, `settings.py`, `context_fold.py`, `nudge.py`, `hooks.py`, `permissions.py`, `gate.py` and `_email_in_headers.py`. The casts package lands as `lionhub.agent.casts`, with its own initializer and row, so neither package initializer overwrites the other.

## Who uses it

- `lionhub/src/lionhub/orchestration/patterns.py` and `lionhub/src/lionhub/orchestration/engines/engine.py`: build agents and compose role specifications.
- `lionagi/cli/agent.py`, `lionagi/cli/orchestrate/_orchestration.py` and `lionagi/cli/orchestrate/fanout.py`: the `li agent` and orchestration front ends.
- `lionagi/tools/coding.py`: imports the factory and specification helpers for coding sub-agents.
- The Studio agent and definition services construct agents through the same path once they consume `lionhub`.

## Status

Moves next in wave 3, together with `tools` and `casts`, in one change. The final import path is `lionhub.agent`.
