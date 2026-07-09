# ADR-0001: lion-studio as Internal Monorepo App

**Status**: Accepted
**Date**: 2026-05-19

## Context

lionagi is evolving from a pure Python SDK into a daily-driver application: the package stays
SDK-stable, but the repository will gain a dashboard (Next.js) and API backend (FastAPI) under
`apps/studio/`. The distribution boundary must be decided before any studio code lands.

Three structural options exist: a separate PyPI package (`lionagi-studio`), a sibling repository
(`lionagi-studio/`), or an internal directory within the lionagi monorepo. The choice affects
release cadence, install ergonomics, and the ability to evolve shared types in lockstep.

## Decision

We place lion-studio under `apps/studio/` inside the lionagi repository. The `lionagi` PyPI
package stays SDK-only; the studio is exposed as `pip install lionagi[studio]`, which pulls in
optional dependencies (Starlette, FastAPI, and related packages). All existing SDK exports
(`from lionagi import Branch, Session, …`) remain unaffected.

## Consequences

**Positive**

- Single repository: types, tests, and CI stay in sync without cross-repo coordination.
- Optional install surface keeps the base SDK lightweight for users who don't need the UI.
- `li studio start` CLI entrypoint integrates naturally with the existing `lionagi.cli` structure.

**Negative**

- The lionagi `pyproject.toml` gains optional dependencies that must be kept current.
- Contributors working only on the SDK must be aware that `apps/studio/` exists and not break it.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Separate `lionagi-studio` PyPI package | Release-cadence drift; users need to align two version pins; more install friction for daily-driver intent |
| Sibling repository `lionagi-studio/` | Split git history; type changes in `lionagi` require coordinated PRs across two repos |

## References

- [ADR-0002](ADR-0002-studio-tech-stack.md) — selects the tech stack for Lion Studio
