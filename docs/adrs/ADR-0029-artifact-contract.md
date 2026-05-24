# ADR-0029: Artifact Contract

**Status**: Proposed
**Date**: 2026-05-23
**Extends**: ADR-0021 (skill artifacts, structured outcomes), ADR-0012 (run-step provenance)
**Related**: ADR-0028 (status reasons — writes `run.failed.missing_artifact`)

## Context

A session that exits cleanly is not the same as a session that delivered
what it was asked to deliver. The runs list currently treats "process
exited with code 0" as success, but several recurring failure classes
look like success at the OS level:

### 1. The reviewer-with-no-output pattern

The recent /codex-pr-review failures had this signature: codex started,
read a few files, returned a short text response, and exited cleanly —
but never wrote `codex_review_pr<N>.md`. The session shows
`status='completed'`, `exit_code=0`. There is no failure to investigate,
because by Studio's current model, nothing failed. The Attention Queue
draft (ADR-0030) calls this "Run failed: No file operations detected"
and surfaces it as a critical row. That phrasing reverse-engineers a
*contract* from a *symptom*. The contract should be explicit.

### 2. Show plays without deliverables

The show skill expects each play to write a verdict and an
implementation brief into `<show>/<play>/`. When an agent finishes early
without writing them, the gate downstream has no input to grade. The
play merges anyway (because gate verdict defaults are permissive), then
the next play in the DAG breaks because the artifact it depended on
never existed.

### 3. The phantom-session "missing_artifacts" reason

ADR-0024's phantom classifier flags sessions whose `artifacts_path`
directory was deleted or never created. This is the post-hoc detection
of the same problem: there was an implicit expectation that artifacts
would be produced, and the cleanup logic doesn't know it should have
checked, so the user sees a phantom badge after the fact instead of a
contract failure at completion.

### 4. ADR-0021 set up the storage; we never set up the contract

ADR-0021 introduced the `artifacts` table for structured `SkillOutcome`
JSON (review verdicts, gate verdicts, CI results) and `file_path` for
large blobs. That ADR is about *storage*: where do artifacts live once
produced. It is silent on the prior question: which artifacts *must*
this session produce, declared up front?

The triggering observation: every Studio surface that needs to detect
"this run was supposed to do X but didn't" is reverse-engineering an
implicit contract. Make the contract explicit at the playbook layer,
snapshot it onto the session at start, verify it at completion, and the
failure mode becomes a first-class status reason instead of a phantom
diagnosis.

## Decision

Add a declarative artifact contract that:

1. Lives in playbook YAML (`artifacts:` block) and optionally in agent
   profile frontmatter (`artifact_defaults:`).

2. Is *resolved* at session start (playbook overlays agent defaults) and
   *snapshotted* onto the session in `artifact_contract_json`.

3. Is *verified* at session teardown by the executor; the verification
   result is written to `artifact_verification_json`.

4. On missing-required failure, the contract *overrides exit code*: the
   session becomes `failed` with reason `run.failed.missing_artifact`
   per ADR-0028, regardless of process exit status.

Existing playbooks with no `artifacts:` block opt out of verification —
no implicit "must produce at least one artifact" default. The contract
is something playbook authors declare deliberately.

### 1. Schema additions

Two columns on `sessions`:

```sql
ALTER TABLE sessions ADD COLUMN artifact_contract_json JSON;
ALTER TABLE sessions ADD COLUMN artifact_verification_json JSON;
```

No new tables. The existing `artifacts` table (ADR-0021) is referenced
for outcome-kind contract checks (deferred to v1.1; see Non-Goals).

`artifact_contract_json` is immutable for the session's lifetime —
written at session creation, never updated. Editing the playbook
mid-run does not retroactively change what was expected.

### 2. Playbook contract shape

```yaml
# ~/.lionagi/playbooks/code-review.playbook.yaml
name: code-review
model: claude/sonnet
prompt: |
  Review the changes in {target} and produce review.md with findings.

artifacts:
  expected:
    - id: review
      path: review.md
      required: true
      description: "Reviewer verdict and findings"
    - id: notes
      path: notes.md
      required: false
      description: "Optional supplementary observations"
```

Fields per expected artifact:

| Field | Type | Default | Description |
|---|---|---|---|
| `id` | str | — | Stable identifier (kebab-case). Used as the merge key and as the evidence reference for status reasons. |
| `path` | str | — | Path *relative to* `sessions.artifacts_path`. No leading `/`, no `..`, no globs (v1). |
| `required` | bool | `true` | Missing required ⇒ run becomes `failed`. Missing optional ⇒ warning, no status change. |
| `description` | str | `""` | Free-text shown in Studio's run detail and in the status reason evidence panel. |

Reserved for v1.1 (see Non-Goals): `kind`, `min_size`, `mime_type`,
`outcome_kind`, glob patterns.

### 3. Agent profile defaults

Agent profiles in `~/.lionagi/agents/<name>/<name>.md` can declare
`artifact_defaults` in frontmatter:

```yaml
---
name: reviewer
model: codex/gpt-5.5
artifact_defaults:
  expected:
    - id: report
      path: report.md
      required: false
---

# Reviewer agent body here…
```

Defaults are role-based ("a reviewer typically produces a report") and
weakly required. Playbooks (which know the task) usually override or
narrow them.

### 4. Resolution rules

At session creation:

```python
def resolve_artifact_contract(
    *, playbook_artifacts: dict | None,
    agent_defaults: dict | None,
) -> dict | None:
    """Merge playbook + agent_profile artifact expectations.

    Returns the resolved contract or None if neither side declares one.
    Verification is skipped entirely when the result is None.
    """
    if not playbook_artifacts and not agent_defaults:
        return None

    by_id: dict[str, dict] = {}

    # Start with agent defaults (lower precedence)
    for spec in (agent_defaults or {}).get("expected", []):
        by_id[spec["id"]] = {**spec, "source": "agent_profile"}

    # Playbook overrides by id; same id wins entirely
    for spec in (playbook_artifacts or {}).get("expected", []):
        by_id[spec["id"]] = {**spec, "source": "playbook"}

    return {"expected": list(by_id.values())}
```

Merge semantics:

- Union by `id`.
- On id collision: the playbook entry replaces the agent default
  entirely (not a deep merge — replacing `required: false` with
  `required: true` is a common case that deep merge would surprise).

- `source` is recorded so the UI can show "expected by playbook" vs
  "expected by agent default" in the run detail view.

### 5. Snapshot at session start

The session creation code in `lionagi/cli/agent.py` and the orchestrate
modules call:

```python
contract = resolve_artifact_contract(
    playbook_artifacts=playbook_spec.get("artifacts"),
    agent_defaults=agent_profile.get("artifact_defaults"),
)
await db.create_session(
    ...,
    artifact_contract=contract,  # serialized to JSON in StateDB
)
```

The session row carries the resolved contract for the rest of the run.
`artifact_verification_json` stays NULL until teardown.

### 6. Verification at teardown

After the user code finishes (success, exception, timeout — all paths),
the CLI teardown runs the verifier *before* the status transition:

```python
# lionagi/state/artifact_verifier.py
import os, time
from typing import TypedDict

class VerificationResult(TypedDict):
    status: str                    # "passed" | "failed" | "warning" | "skipped"
    checked_at: float
    missing_required: list[dict]   # entries from the contract
    missing_optional: list[dict]
    produced: list[dict]           # {id, path, size, present: True}

def verify_artifact_contract(
    contract: dict | None,
    *,
    artifacts_root: str | None,
) -> VerificationResult | None:
    if contract is None:
        return None  # nothing declared → nothing to check
    if not artifacts_root or not os.path.isdir(artifacts_root):
        return {
            "status": "failed",
            "checked_at": time.time(),
            "missing_required": [
                e for e in contract["expected"] if e.get("required", True)
            ],
            "missing_optional": [
                e for e in contract["expected"] if not e.get("required", True)
            ],
            "produced": [],
        }

    missing_required, missing_optional, produced = [], [], []
    for entry in contract["expected"]:
        full = _safe_join(artifacts_root, entry["path"])  # see below
        present = os.path.isfile(full) and os.path.getsize(full) > 0
        if present:
            produced.append({
                "id": entry["id"],
                "path": entry["path"],
                "size": os.path.getsize(full),
            })
        elif entry.get("required", True):
            missing_required.append(entry)
        else:
            missing_optional.append(entry)

    if missing_required:
        status = "failed"
    elif missing_optional:
        status = "warning"
    else:
        status = "passed"

    return {
        "status": status,
        "checked_at": time.time(),
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "produced": produced,
    }
```

"Produced" requires the file to exist *and* be non-empty. A zero-byte
`review.md` is not a delivery.

Path resolution uses a `_safe_join()` that enforces the rules declared
in Section 2 ("No leading `/`, no `..`, no globs"). The verifier MUST
NOT use bare `os.path.join`, which is permissive about absolute paths
and `..` escapes:

```python
import os
from pathlib import PurePosixPath

_GLOB_CHARS = frozenset("*?[]")

class ArtifactPathError(ValueError):
    pass

def _safe_join(root: str, rel: str) -> str:
    """Join ``rel`` under ``root``, rejecting paths that escape the root.

    Rejects absolute paths, `..` segments, glob metacharacters, and
    any final path that does not have ``root`` as its prefix after
    realpath resolution. ``root`` itself is expected to already be
    realpath-resolved and absolute.
    """
    if not rel or rel.startswith("/"):
        raise ArtifactPathError(f"absolute path not allowed: {rel!r}")
    if any(c in _GLOB_CHARS for c in rel):
        raise ArtifactPathError(f"glob characters not allowed in v1: {rel!r}")
    parts = PurePosixPath(rel).parts
    if any(p in ("..", "") for p in parts):
        raise ArtifactPathError(f"`..` segments not allowed: {rel!r}")
    joined = os.path.realpath(os.path.join(root, *parts))
    if joined != root and not joined.startswith(root + os.sep):
        raise ArtifactPathError(f"path escapes artifacts_root: {rel!r}")
    return joined
```

The same validation runs at session start as `validate_artifact_contract(contract)`,
so a bad path fails fast rather than at teardown:

```python
def validate_artifact_contract(contract: dict | None) -> None:
    if contract is None:
        return
    seen_ids: set[str] = set()
    for entry in contract["expected"]:
        eid = entry["id"]
        if not eid.replace("-", "").replace("_", "").isalnum():
            raise ArtifactPathError(f"id must be alphanumeric/_/-: {eid!r}")
        if eid in seen_ids:
            raise ArtifactPathError(f"duplicate id in contract: {eid!r}")
        seen_ids.add(eid)
        # Pre-flight path check: realpath against a dummy root catches
        # the rule violations without needing the live artifacts_path.
        _safe_join("/tmp/__contract_validate__", entry["path"])
```

`artifacts_path` is the existing column on `sessions` (ADR-0012). No
new path concept introduced.

### 7. Failure precedence vs exit code

The contract verifier runs first; its outcome may change the status the
CLI was about to set:

```python
# lionagi/cli/agent.py teardown, simplified
proposed_status, proposed_reason = _resolve_status(exception, exit_code)
verification = verify_artifact_contract(
    contract=session.artifact_contract,
    artifacts_root=session.artifacts_path,
)

if verification and verification["status"] == "failed":
    if proposed_status == "completed":
        # Override: a clean exit without required artifacts is still a failure
        final_status = "failed"
        final_reason_code = RunReasons.FAILED_MISSING_ARTIFACT
        final_reason_summary = _format_missing(verification["missing_required"])
        final_evidence = [
            {"kind": "expected_artifact", "id": e["id"], "label": e["path"]}
            for e in verification["missing_required"]
        ]
    else:
        # Already failing — keep the original cause, note the contract
        # violation in metadata only. Don't overwrite a more specific cause.
        final_status = proposed_status
        final_reason_code = proposed_reason["code"]
        final_reason_summary = proposed_reason["summary"]
        final_evidence = proposed_reason["evidence_refs"]
else:
    final_status = proposed_status
    final_reason_code = proposed_reason["code"]
    final_reason_summary = proposed_reason["summary"]
    final_evidence = proposed_reason["evidence_refs"]

await db.update_artifact_verification(session_id, verification)
await db.update_status(
    entity_type="session",
    entity_id=session_id,
    new_status=final_status,
    reason_code=final_reason_code,
    reason_summary=final_reason_summary,
    evidence_refs=final_evidence,
    source="executor",
)
```

Precedence rules:

1. If the run was already going to fail (exception, timeout, abort,
   non-zero exit), the *original* cause is preserved in the status
   reason. The verification result is recorded in
   `artifact_verification_json` for the UI to surface but does not
   overwrite the reason. A run that crashed and *also* missed
   artifacts is still primarily "crashed".

2. If the run was going to be `completed` but verification failed on a
   required artifact, the status flips to `failed` with reason
   `run.failed.missing_artifact`. This is the case the contract
   primarily exists to catch.

3. Optional artifacts never change the status. They produce a
   `warning` verification result; the UI surfaces it as a yellow chip
   on the run detail page.

### 8. Studio rendering

Run detail page gains an "Expected artifacts" section that shows the
contract + verification side by side:

```text
Expected artifacts                          Verified: failed

  REQUIRED  review        review.md         MISSING
            description: Reviewer verdict and findings
            declared by: playbook

  REQUIRED  report        report.md         OK (1.2 KB)
            declared by: agent_profile

  OPTIONAL  notes         notes.md          MISSING
            description: Supplementary observations
            declared by: playbook
```

Each row links to:

- the file if produced (`file_path` opens the editor / artifact viewer)
- the status reason if missing (the entry's `id` matches the
  `evidence_refs.id` from ADR-0028, so clicking jumps to the failure
  reason)

When no contract was declared, the section is hidden entirely (no
"contract: none" placeholder — silence beats noise).

### 9. CLI tooling

A pre-flight check helps playbook authors:

```text
li play check code-review
  → contract resolved (3 expected: 2 required, 1 optional)
  → all paths relative-OK
  → no id collisions with agent_profile defaults
```

A post-run summary appears in the CLI on `failed` due to contract:

```text
Run failed: missing required artifacts.
  review (review.md) — playbook
  report (report.md) — agent_profile

Run completed cleanly otherwise. Inspect with:
  li state show-session <id>
```

### 10. Migration

- `ALTER TABLE sessions ADD COLUMN artifact_contract_json JSON;`
- `ALTER TABLE sessions ADD COLUMN artifact_verification_json JSON;`
- Existing rows: both NULL. The verifier reads NULL contract as
  "skipped", so historical runs behave exactly as before.

- Playbooks without `artifacts:` are unchanged in behaviour.
- Agent profiles without `artifact_defaults` in frontmatter are
  unchanged in behaviour.

### 11. File map

New files:

```text
lionagi/state/artifact_verifier.py        # resolve + verify functions
```

Modified files:

```text
lionagi/state/schema.sql                  # 2 ALTERs on sessions
lionagi/state/db.py                       # create_session(artifact_contract=...),
                                          # update_artifact_verification()
lionagi/cli/orchestrate/__init__.py       # parse playbook artifacts block
lionagi/cli/orchestrate/_orchestration.py # snapshot contract at session start
lionagi/cli/agent.py                      # teardown runs verifier before status update
lionagi/cli/_agents.py                    # AgentProfile parser exposes
                                          # `artifact_defaults` from frontmatter
                                          # (see lionagi/cli/_agents.py:160 for
                                          # current frontmatter dispatch)
apps/studio/server/services/sessions.py   # include contract+verification in API
apps/studio/server/routers/runs.py        # detail endpoint exposes both
apps/studio/frontend/components/runs/ExpectedArtifacts.tsx  # new UI section
```

## Consequences

**Positive**

- The reviewer-with-no-output pattern becomes a first-class failure with
  a stable reason code, not a phantom diagnosis after the fact.

- Playbook authors can declare deliverables explicitly; agents have
  permission to ignore optional ones; required ones are enforced
  uniformly without each playbook re-implementing checks in its prompt.

- The "Missing artifacts" phantom-session class collapses: contract
  failures are caught at completion, so the cleanup heuristic doesn't
  have to detect them post-hoc.

- The status reason `run.failed.missing_artifact` (ADR-0028) carries
  the exact list of unmet artifact IDs as `evidence_refs` — Studio can
  link directly to "what's missing" instead of "something is missing".

- Backwards-compatible by default. Existing playbooks behave exactly as
  before until they opt in.

- The `artifacts` table from ADR-0021 still does what it did (structured
  outcome storage); this ADR adds the *contract* layer that ADR-0021
  was missing.

**Negative**

- Two new columns on `sessions` table. Already a wide table; the
  alternative was a separate table that would have required a JOIN on
  every session detail render.

- Path-based verification is conservative — it only checks file
  existence and non-emptiness. A 1-byte `review.md` containing just
  "X" passes, even though it is clearly garbage. Content validation is
  deferred to v1.1.

- Glob patterns deferred. `notes/*.md` won't match anything in v1. A
  playbook that needs "any number of notes files" has to list them or
  wait for v1.1.

- Outcome-kind contracts deferred. A playbook can't say "must produce
  a `review_verdict` artifact row" — only "must produce a file at this
  path". Outcome-kind verification needs ADR-0021's artifact writers
  to be standardized first.

- Agent-default declarations require editing the agent profile
  frontmatter, which is markdown YAML — easy to typo. The pre-flight
  check (Section 9) is the safety net.

- The 1-line precedence rule ("contract failure overrides clean exit
  but not a more specific failure") will be a small learning curve.
  The fix is to make the UI show both signals when they disagree:
  status reason + verification banner.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Default to "must produce at least one artifact" when no contract declared | Many existing playbooks are legitimately diagnostic (research, exploration, dry-run). Defaulting to artifacts-required would break the marketplace's `research.playbook.yaml`, `pr-review.playbook.yaml`, and others. Opt-in is the right default. |
| Contract lives only in playbooks (no agent defaults) | Agent roles like reviewer, auditor, analyst have stable output expectations. Letting an agent profile declare its default deliverables lets a generic playbook just say "use the reviewer agent" and inherit a sensible contract. |
| Contract lives only on agent profiles (no playbook) | Playbooks know the task; agents are reusable across tasks. Task-specific deliverables (a specific `pr-1070-review.md`) belong on the playbook. Agent defaults are role-based; playbooks are task-based. |
| Verify in a separate process / service | Adds infra. Completion-time verification in the executor is simpler, runs in the same Python process that knows the contract and the artifacts path, and lets us write the verification result transactionally with the status reason. |
| Store contract as a row per artifact in a new `artifact_contracts` table | A session has 1-5 expected artifacts. A row per artifact would multiply session rows × ~3. JSON column on the session row keeps the snapshot local, single-fetch, and matches how `plays.depends_on` is stored. |
| Glob patterns from v1 | Glob raises hard questions: any-of-match vs all-of-match, count constraints, ordering. Concrete paths cover the 80% case. Add glob in v1.1 once the contract shape has settled. |
| Verify at gate-time instead of completion-time | Gates already run per-play in `show` and don't run at all in regular `li agent` / `li play`. Completion-time verification works for every CLI surface. Gate-time verification can be added in v1.1 (gate inputs would include `verification.passed`). |
| Treat all missing artifacts as warnings | Defeats the point. The whole reason this ADR exists is to *fail* runs that didn't deliver. Required must be enforceable. |

## Non-Goals

- **No glob patterns.** v1 supports concrete paths only.
- **No content validation.** The verifier checks existence and non-zero
  size; it does not validate markdown structure, JSON schema, or file
  type by mime.

- **No `kind: outcome` contracts.** Declaring "must produce a
  `review_verdict` artifact row in the `artifacts` table" is deferred
  to v1.1, after ADR-0021 outcome writers are standardized.

- **No gate-time verification.** v1 verifies once, at session
  teardown.

- **No retroactive backfill.** Sessions created before this ADR have
  NULL contract and are unverified.

- **No quality scoring.** A file that exists passes. Quality (length,
  structure, validity) is out of scope.

- **No automatic artifact discovery.** The contract is declared, not
  inferred. There is no "scan all .md files in artifacts_path and treat
  them as produced" — playbooks declare what they expect.

- **No artifact diffing or version history.** ADR-0021's `artifacts`
  table holds versions; the contract just verifies "exists, non-empty".

## References

- [ADR-0012](ADR-0012-studio-execution-lineage.md) — Source of `sessions.artifacts_path` column.
- [ADR-0021](ADR-0021-skill-artifacts-and-reactive-chaining.md) — Existing `artifacts` table for structured skill outcomes; this ADR adds the contract layer.
- [ADR-0024](ADR-0024-session-health-and-admin-surface.md) — The `missing_artifacts` phantom reason collapses into this contract's `missing_required` outcome.
- [ADR-0028](ADR-0028-status-reason-model.md) — Writes `run.failed.missing_artifact` with `evidence_refs` pointing at unmet artifact IDs.
- [ADR-0030](ADR-0030-attention-queue.md) — Consumes contract-failure attention items.
- `lionagi/cli/agent.py` — Session teardown (modify to call verifier before status update).
- `lionagi/cli/orchestrate/__init__.py` — Playbook spec parsing (add `artifacts:` field).
- ChatGPT frontend design review (external) — proposed an artifact contract; this ADR grounds the proposal in the existing `sessions.artifacts_path` and `artifacts` infrastructure (which the proposal didn't know about).

### Prior art

- **GitHub Actions `actions/upload-artifact`** — declares expected artifacts at workflow level; the workflow fails if `if-no-files-found: error` and the artifact path is empty. Direct inspiration for the required/optional split.
- **Docker `HEALTHCHECK`** — completion-time check that runs after the container's main process, declares the contract that "the service is up", failure flips the container state. Same shape: declared contract, runtime verifier, status flip on miss.
- **pytest `--require-pyx`-style fixtures** — declarative "this test requires X to exist" with explicit failure when the precondition isn't met. Conceptually parallel.
