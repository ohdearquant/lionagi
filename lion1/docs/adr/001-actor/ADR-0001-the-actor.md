---
adr: ADR-0001
status: draft
liveness: operating (the profile's `defaults` and the `run.more` handler are owed with ADR-0003)

date: "2026-09-23"
area: actor
kind: new
depends_on: []
tags:
  - adr
  - runtime
---

# ADR-0001: The actor and its privileges

## Context

An agent is software that solves a problem on its own, and its harness is what lets it act on an
environment and keep state. Agents do more when several work together, and as their number grows
each one has to be identifiable and distinct from the others, or there is no way to control any of
them precisely. This record fixes that unit: one named party, what it may be asked, and what it is
permitted to do.

The run is [[ADR-0002-the-run|ADR-0002]], the job and its bounds [[ADR-0003-the-bounds|ADR-0003]],
the order a command goes through [[ADR-0005-command-handling|ADR-0005]]. Source: `lionagi/spec.py`
and the actor half of `lionagi/actor.py`, in the v1 implementation that follows these records into
this repository.

## Definitions

- **actor**: one addressable principal at runtime.
- **request**: a signal the model produces that the system can react to; in the language it is a
  command (`<lact>`), a value declaration (`<lvar>`) or the output (`OUT{}`)
  ([[ADR-0004-the-language|ADR-0004]]).
- **Spec**: the named type of a request, modelled as a Pydantic class with descriptions.
- **handler**: how the system reacts to a request, matched by the Spec the model asked for.
- **Operable**: the allowed space of Specs an actor can work with.
- **privilege**: a name that says whether an actor may reach a handler or a resource at runtime; it
  has no meaning beyond the handlers that require it.
- **profile**: a configuration of the model for one run of an actor: a name, a subset of the
  Operable, a subset of the actor's privileges, the role's prose, optionally a note store, and
  `defaults` for the job's bounds.
- **prompt**: the instruction to the model, controlled by the harness.

## Assumptions

| #  | statement                                                                                                  | source                                                                                     | if false                                          |
| -- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------- |
| A1 | A Pydantic class's name, docstring and field descriptions are enough guidance for a model to call it well. | 50-instance SWE-bench runs: five Specs, 1976 calls in 952 turns, 3 output field errors    | C1 needs a second guidance form beside the class  |
| A2 | A command never presented is attempted rarely enough that the runtime refusal is a backstop, not a gate.   | assumption; unknown-name refusals were not counted on the bench                            | C5's prompt is no longer the first gate           |
| A3 | Privileges are few and named by the operator; a handler's `requires` is read by a person, not computed.    | the code holds opaque strings and set inclusion, nothing more | the names need an owner and a review; C4 is unchanged |

## Claims

### C1: The Spec class is the only guidance and the only validator _(enforced: mechanical)_ ^c1

- **Subject**: every Spec registered on an actor.
- **Violated when**: a schema not derived from the class describes a Spec, or a handler's arguments
  are validated by anything but the class.

A Spec's name is `__spec_name__` when the class sets one, else the snake_case of the class name.
`render_guidance` prints the name, the docstring's first line, and each field with its type, default
and description; that text is the whole of what the model is told about the Spec. At dispatch the
same class validates the arguments with `model_validate`, and a validation error is the refusal text
the model reads next turn.

### C2: The Operable is exactly the handler set, and a name is taken once _(enforced: mechanical)_ ^c2

- **Subject**: every actor.
- **Violated when**: a name is in the Operable with no handler, a handler is registered without its
  Spec entering the Operable, or two classes register under one name.

`register` adds the Spec and the handler in one step; a second class under a taken name is refused.
The default handlers arrive with the actor: the context commands, the note commands
([[ADR-0007-the-record|ADR-0007]]); `run.more` joins them when it lands
([[ADR-0003-the-bounds|ADR-0003]]). What is not in the Operable does not exist in the model's world.

### C3: A profile never exceeds its actor, and is checked before the first turn _(enforced: mechanical)_ ^c3

- **Subject**: every run.
- **Violated when**: a profile naming a Spec outside the Operable or a privilege the actor lacks is
  admitted to a run, or a profile carries an address, credentials or a model of its own.



`Actor.check` refuses, with `ProfileError`, one whose Specs are not in the Operable or whose
privileges are not a subset of the actor's; the check runs before any turn. A profile whose `specs`
is None presents the actor's whole Operable, so only its privileges are checked.

The profile has no address, no credentials and no model; the backend is the caller's argument to
`run`. Two profiles running at once are two concurrent runs of one actor: they share its registry,
its bus and its note stores, and nothing serialises them. A job value set explicitly overrides the
profile's `defaults` ([[ADR-0003-the-bounds|ADR-0003]]).

### C4: A handler runs only under a profile holding what it requires _(enforced: mechanical)_ ^c4

- **Subject**: every dispatched command whose handler has a non-empty `requires`.
- **Violated when**: a handler runs under a profile missing one of its privileges, or the privilege
  check runs before the arguments are validated or after a hook.

The check compares the handler's `requires` with the run's profile's privileges, after validation
and before any hook, so a privilege refusal names a well-formed request and a hook never sees a
command the profile could not run. The refusal carries `reason="privilege"` and the missing names,
and the model reads it next turn.

### C5: The prompt is generated from the profile's subset alone _(enforced: mechanical)_ ^c5

- **Subject**: every run.
- **Violated when**: the prompt's COMMANDS section names a command outside the profile's subset, or
  a name in the subset is missing from it.

The prompt is the profile's prose, then the LNDL guidance, then COMMANDS rendered from the subset's
Specs, then OUTPUT when the job declares `emits`. The profile's prose is the operator's and may say
anything; the guarantee is over the generated section. A name outside the subset is never presented;
the runtime refusal of an unknown name ([[ADR-0005-command-handling|ADR-0005]]) is the backstop. Two
gates, then: the subset decides what is presented, and privilege (C4) decides what a presented name
may do.

Both gates hold over the commands the runtime dispatches. A backend given tools of its own acts
outside them ([[ADR-0009-backends#^c6|ADR-0009/C6]]).

## Decisions

### D1: Specs enter the Operable by handler registration; profiles are checked at run start ^d1

Serves C2 and C3. `Operable.add` refuses name collisions, `Operable.subset` builds a profile's
registry in registration order, and `Actor.check` raises `ProfileError` for over-asking profiles
before any turn.

- **Landing evidence**: `tests/test_actor.py`, `test_a_subset_named_by_a_generator_is_read_once`;
  its one `ProfileError` case is a name. Owed: a test that a profile naming a Spec outside the
  Operable, or a privilege beyond the actor, raises `ProfileError` before any turn, and one that
  `Operable.add` refuses a taken name.

### D2: The system prompt is generated in four parts ^d2

Serves C5. `_render` joins the profile's prose, the LNDL guidance, COMMANDS from the subset's
guidance, and the output guidance when the job declares `emits`. Nothing else enters the system
message.

- **Landing evidence**: the first backend call of any recorded run, whose system message is those
  parts.

### D3: Privilege is checked once per command, after validation and before the hooks ^d3

Serves C4. `Handler.requires` is a frozenset; the dispatch path validates the arguments, then
compares `requires` with the profile's privileges, then runs the before hooks.

- **Landing evidence**: `tests/test_actor_execution.py`, the privilege cases; the `Result.reason`
  vocabulary in `lionagi/actor.py`.

## Alternatives

| approach                                                | rejected because                                                                                     |
| ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| A framework-neutral field IR with per-provider adapters | more than this runtime needs; the class already is the guidance and the validator                    |
| A runtime refusal as the only gate                      | a presented name that fails costs the model a turn; a name not presented costs nothing              |
| A profile as a principal with its own address           | two profiles are two concurrent runs of one actor; credentials and the inbox belong to the actor     |
| Privileges as a hierarchy or a policy language          | three sets and inclusion answer every question the runtime asks; a policy is the operator's, outside |
| Privileges on the profile alone                         | a profile could then hold what its actor was never given; the actor is the principal                 |

## Consequences

- **S1**: Adding a capability is one class and one function; nothing in the loop changes. Pydantic
  is the runtime's one dependency.
- **S2**: Everything the model may say is readable from its prompt; a presented name is never
  refused as unknown, and validation, privilege and the before hooks still gate it
  ([[ADR-0005-command-handling|ADR-0005]]).
- **S3**: A privilege's meaning lives in the handlers that require it and the operator who grants
  it; the runtime enforces inclusion and nothing else.
- **S4**: Validation precedes the privilege check, so a Spec's validators run for a request the
  profile could not run. A validator is therefore shape only: local, effect-free, reading nothing
  outside its arguments; whether a path exists, an id is the caller's or a service answers is the
  handler's question, behind the gate. A validator is also stable: built again from a canonical
  instance's fields it yields the same fields, so how many times a Spec was built changes nothing
  (ADR-0005/C4, S8).
