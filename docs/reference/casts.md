# casts — reference

## Role fields: prompt vs. orchestrator

`Role` has two text fields with distinct routing:

- `body`: included in the system prompt sent to the model.
- `description`: orchestrator-facing selection signal only; never injected into the prompt.
- `emits`: tuple of Pydantic model classes declaring the role's emission contract (see `casts.emission`).

## Roles and Modes are a closed built-in set

Built-in roles and modes live under `lionagi.casts.roles` and `lionagi.casts.roles.modes`. They are not user-definable. To override runtime behavior (model, effort, permitted modes, authority, escalation targets), supply a custom `Pack` — not a new role module.

## Mode conflict declarations

A `Mode` may declare `conflicts_with: frozenset[str]`. `Profile.__post_init__` raises `ValueError` if any two active modes conflict. Declare conflicts symmetrically in both mode definitions to be safe; the check is bidirectional.

## Emission contract: EscalationRequest injection

`build_emission_operable` always appends `EscalationRequest` to the emits tuple if not already present. Every emitting role can therefore escalate, regardless of its declared `emits`.

## Pack overlay precedence

A `Pack` overlays per-role `RolePolicy` (authority, boundaries, escalations) and `RoleConfig` (model, effort, modes_allow, active). The built-in `default.yaml` ships with lionagi; pass a custom `Pack` to the orchestrator to override or extend it. Pack fields do not affect the prompt body.
