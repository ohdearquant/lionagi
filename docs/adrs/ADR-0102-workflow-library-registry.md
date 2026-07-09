# ADR-0102: Workflow Library Registry

**Status**: Accepted
**Date**: 2026-07-08

Depends on: ADR-0016 (definitions write path). Composes with ADR-0101 at exactly one
seam: `TaskApplication.library_ref` plus the task row's
`(library_ref, library_content_hash)` provenance pin.

## Context

Reusable orchestration currently lives in THREE disconnected stores:

1. Filesystem playbooks — `~/.lionagi/playbooks/<name>.playbook.yaml`, flat, no
   versions, no namespace; bundled built-ins install idempotently
   (`lionagi/studio/services/playbooks.py`).
2. The `definitions` table — already versions content monotonically per
   `(kind, name)` with `kind IN ('agent','playbook')` (`lionagi/state/schema.sql`),
   but it is an edit-history feature: `li play` resolution never reads it.
3. The `workflow_defs` table — named spec_json, AST-restricted compile, runs through
   `Session.flow` (`lionagi/studio/services/workflow_run.py`), but `name UNIQUE`
   with NO version and NO namespace.

Consequences: no `name@version` pin at invoke time, no provenance from a run back to
the library version it executed, and no shared contribution surface for teams to
publish reusable workflows to.

## Decision

### D1. `definitions` is the registry of record

- Widen `definitions.kind` CHECK to `('agent','playbook','workflow')`.
- Add a `namespace` column (explicit column, not a name convention); uniqueness
  becomes `(kind, namespace, name, version)`. Default namespace for existing rows:
  `core`.
- Fold `workflow_defs` into it: migrate rows as `kind='workflow'` version 1, then
  retire the `workflow_defs` table (its name-UNIQUE-no-version shape is what is
  being replaced). `workflow_compile` / `workflow_run` read from `definitions`.

### D2. Two formats, ONE registry

Playbook YAML (prompt-shaped) and workflow spec_json (DAG-shaped) remain distinct
formats, discriminated by `kind`. No canonical-DSL rewrite: the registry unifies
discovery, versioning, and provenance — not syntax.

### D3. Authoring/contribution surface = a workflows git repository

A git repository of workflow/playbook definitions is where teams contribute; PR
review is the contribution gate. An idempotent installer syncs repository content
into `definitions` — the exact pattern bundled playbooks already use
(`install_builtin_playbook`). Filesystem playbooks become an import/export format,
not a third source of truth.

### D4. Invoke-time resolution + provenance

- `li play name@version` (and `library_ref = "namespace/name@version"` in a
  `TaskApplication`) resolves through `definitions`; a bare `name` resolves the
  latest version in the default namespace (current behavior preserved).
- At submit/run, the run row pins `(kind, namespace, name, version)` +
  `content_hash` (ADR-0101 columns `library_ref`, `library_content_hash`). This
  closes the run→library-version provenance gap.

## Alternatives rejected

- Promote `workflow_defs` (add version/namespace there): re-implements the
  versioning `definitions` already has, and stays disjoint from agent/playbook
  history. Rejected.
- One canonical format: forces a rewrite of every existing playbook for zero
  provenance gain. Rejected.
- Filesystem/git as the store of record with the DB as a cache: loses transactional
  resolution and provenance pinning at invoke; git stays the AUTHORING surface, the
  DB the RECORD. Rejected.

## Scope fence (v1 MUST NOT contain)

No new DSL; no marketplace/discovery UI beyond list/get; no cross-registry
federation; no signing/attestation of definitions (later, if the repository review
gate proves insufficient); no changes to playbook YAML or workflow spec_json syntax.

## Verify by

1. `li play name@version` resolves through `definitions` to the exact version row.
2. A run records the exact library version + content hash it executed.
3. A repository install lands a new version row idempotently (re-install = no-op).
4. `workflow_defs` consumers (compile/run/routes) work unchanged against the
   migrated rows; the old table is gone.
5. Schema parity set updated in the same PR (`schema.sql`, `schema_meta.py`,
   engine-schema + route-registry test expectations).
