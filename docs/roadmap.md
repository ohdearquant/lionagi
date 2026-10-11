# Roadmap

lionagi's underlying architecture is migrating towards lion1, a runtime
developed alongside lionagi in the
[`lion1/`](https://github.com/ohdearquant/lionagi/tree/main/lion1) directory of
this repository. lionagi stays the package you install and import. lion1 is not
released as a package of its own, and there is no separate import path. The
decision, its reasoning and the alternatives it rejected are recorded in
[ADR-0125](adr/ADR-0125-bringing-the-lion1-runtime-into-lionagi.md).

## Release target

lionagi v1.0 is planned for around the end of October 2026.

## Compatibility

The migration is backward compatible.

- Public imports, supported call shapes, return types and observable behavior
  stay as they are. New capabilities arrive as additions.
- When an existing name's implementation moves onto a new mechanism, the name
  becomes an adapter and stays supported. It does not emit a
  `DeprecationWarning` just because it now delegates.
- Removing a public name or behavior, or changing what a default install can
  do, needs its own versioned compatibility decision. Where the new mechanism
  cannot reproduce an existing behavior, the adapter keeps the old behavior,
  and changing it waits for its own compatibility decision.
- A `Branch` with no requested persistence keeps today's in-memory behavior.
  When persistence is requested, lionagi's own SQLite state store stays the
  default. External stores plug in as adapters, and lionagi ships no store
  binary.
- Code brought in from lion1 must run on Python 3.10, lionagi's minimum
  supported version, and be typed on every public surface.

## The seven steps

The steps land in this order. Each one opens with its own design record, or an
amendment to an existing one, and a step starts when the previous step's record
is accepted and its first change has merged.

| Step | What changes |
|------|--------------|
| 1. One handler mechanism | Tools, hooks and observers become adapters over one path for commands. The `Branch` holds the authority to run them, interception runs before and after each one, and each returns a typed result. Events go out on a separate fan-out that cannot authorize or veto a command. The existing tool, hook and observer APIs keep working. |
| 2. Directives and context management | What each model call contains is compiled in one place. Hide, show and summarize directives and context folding are built on an internal append-only conversation record. Memory is reached through handlers, and the existing memory store contract stays as it is. Notes (keyed, scoped records with exact reads) arrive as an optional capability beside it. |
| 3. LNDL structured output | [LNDL](adr/ADR-0051-lndl-language-and-operations-boundary.md), lionagi's tag language for structured output, becomes an optional format with one implementation. |
| 4. Dedup and condense | Duplicate mechanisms left after steps 1 to 3 are removed, and optional install profiles are prepared. Changing what a default install includes is outside this roadmap and needs its own compatibility decision. |
| 5. `Agent` | A public `Agent` API over an internal actor: identity, privileges, bounded runs and a typed model-call result. |
| 6. Served agents | An agent can be served over HTTP, on loopback by default, and Studio can start, follow and stop served agents. |
| 7. Long-running agents | Agents that wake on an inbox carried by lionagi's own state store, shown in Studio. |

## How a step lands

Each step lands as a series of changes in this order:

1. Characterization tests pin the behavior of every API the step replaces.
   They are written against the code as it is and pass before anything
   changes.
2. The new mechanism lands with adapters for the existing APIs, and the
   characterization tests still pass unchanged.
3. Code inside lionagi moves onto the new mechanism.

Removing an adapter is not part of any step. The `lion1/` directory stays as
the reference implementation until the last step that draws on it has landed,
and is then removed in one change.

## Progress

Step 1 is tracked in
[#3457](https://github.com/ohdearquant/lionagi/issues/3457), which lists its
design record and characterization tests.
