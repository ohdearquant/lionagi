# ADR-0103: Fixed-flow workflow engine v1.1 contract (li flow run, per-node cwd, per-node model, artifact_dir)

**Status**: Accepted (Leo sign-off 2026-07-09)
**Date**: 2026-07-09

Depends on: ADR-0102 (workflow library registry — `definitions` is the registry of record; runs pin `library_ref` + `library_content_hash`). Composes with the existing Studio workflow-defs engine (`workflow_compile.py` / `workflow_run.py`, the option-B ruling of the earlier "Studio WorkflowDef compile bridge" advisory). Does NOT touch `li play` / the reactive planner lane.

## Context

Ocean directed a reusable, deterministic issue-pipeline (kdev `issues prepare`: codex analyze → sonnet implement → opus critic). The engine owner (lambda:lionagi, accepted by Leo 2026-07-09) ruled to build on the existing Studio workflow-defs engine rather than a new file runner. kdev (khive's CLI) will build its command surface against two engine surfaces that must be pinned durably BEFORE kdev builds: the per-node execution semantics (cwd, model) and the artifact-file output contract. This lane is fixed DAGs only — no planner, no reactive expansion.

The engine as it exists today (source-verified 2026-07-09):

- `workflow_compile.py` compiles a spec into an `OperationGraph`. Chat nodes compile to `chat_and_record(instruction=prompt)` — **`config.model` is validated and displayed but never applied at execution** (issue #1922, confirmed at line 309). Engine nodes DO apply `config.get("model") or defn.get("model")` (line 363). Executable kinds: `input`, `chat`, `engine`; `parse`/`fanout`/`gate` are `DROPPED_NODE_KINDS` that fail a saved def with a structured `WorkflowCompileError`.
- `workflow_run.run_workflow_def(def_id, inputs)` is a plain library async fn: fresh `Session()`, compile, `session.flow(graph, context=inputs, on_progress=…, on_branch_created=…)`, StateDB persistence via a **request-scoped** connection (deliberately not the process-wide singleton — see its module docstring), per-node lifecycle signals via `flow_progress_signals`. `run_id` = session id, so runs already appear in Fleet/History/RunDetail. No daemon required.
- `_teardown_run_persist` already calls `_teardown_common(artifacts_path=…, artifact_contract=…)` — the artifact seam exists; workflow_run currently passes `None`/`None` (lines 100-101).
- CLI providers already carry workspace fields: `claude_code.py` enforces `cwd` within repo bounds **only under `bypassPermissions`** (line 392-402); `codex.py` has `repo`. Per-node cwd is config-passthrough, not provider work.
- `iModel(model="…")` with no explicit provider: a `"provider/model"` string splits on `/` (imodel.py:66-69); a bare string falls back to `settings.LIONAGI_CHAT_PROVIDER` (default `openai`, lines 70-73) — a silent mis-binding, not a hard error at construction.
- `flow_progress_signals._on_progress(op_id, name, status, elapsed)` carries **no result payload** (flow_signals.py:76). Node results are only assembled in the `session.flow()` return dict `operation_results`.
- Persistence precedent: `~/.lionagi/runs/{run_id}/artifacts/` (`_runs.py`).

## Decision

Four slices ship as normal-gate PRs in dependency order (#1922 model fix → per-node cwd → `li flow run` → artifact_dir). This ADR pins the CONTRACTS kdev builds against; it does not pin implementation internals.

### D-F4 — Per-node model (#1922 fix)

- Chat nodes apply `config.model` by constructing an `iModel` and passing it to `chat_and_record` as `chat_model=` (which forwards through `**kwargs` → `chat()`'s `imodel=` param). Engine nodes keep their existing `config.get("model") or defn.get("model")` path unchanged.
- **Model strings MUST be provider-prefixed (`provider/name`, e.g. `openai/gpt-4.1-mini`, `claude/sonnet`). A bare string (no `/`) is REJECTED at compile with a `WorkflowCompileError` naming the node and the required form.** Rationale: a bare string does not fail loudly — it silently binds to the default provider (`openai`), which for `claude/sonnet` intent produces a wrong-provider call that fails at runtime, not compile. Rejecting at compile turns a silent-green trap into an authoring-time error.
- Inheritance is TWO levels, kind-specific:
  - chat: `node.config.model` → branch default (no def-level default introduced in v1.1).
  - engine: `node.config.model` → `engine_def.model` (already exists).
- Migration: because `config.model` was never applied for chat nodes, no existing def was actually depending on a bare model resolving. A saved def that *omits* `config.model` keeps running on the branch default exactly as before. A saved def that *sets a bare* `config.model` gets a clear compile error (allowed by the "keep loading OR fail with a clear compile error" constraint — silent behavior change is what is forbidden, and reject-at-compile is not silent). Validation is added at BOTH the write path (`_validate_chat_config`, fast feedback) and the compile path (authoritative — it catches rows already in the DB).

### D-F1 — Per-node cwd

- The run carries a `base_dir` (absolute) as a **run-level INPUT** (never a spec field — see D-F5).
- A node carries `config.cwd` (a string): absolute, or relative and resolved against `base_dir`.
- Containment: `has_traversal(raw_cwd)` rejects `..`/traversal on the raw string BEFORE resolution; resolution is **symlink-resolving** — `Path.resolve()` on the joined path, not `os.path.normpath` (normpath passes the traversal check while missing a symlink escape); the resolved cwd MUST satisfy `resolved_cwd.relative_to(base_dir.resolve())` (the exact check `claude_code._check_constraints` uses at line 396) AND must exist AND must be a directory — else `WorkflowCompileError` (static cwd) or a structured run error (input-derived).
- This is option (b) refined: `base_dir` + contained node cwds, but node cwds may be ABSOLUTE (not forced-relative), so kdev passes the absolute worktree paths `git worktree add` already gives it without computing relative subpaths. The pipeline "analyze in repo A, implement in a worktree of repo A" is expressed by setting `base_dir` to the common ancestor of the repo and its worktrees (kdev creates the worktrees and owns their placement, so it guarantees a common ancestor).

### D-F2 — Artifact output contract

- `artifact_dir` is an **optional** run-level input. Canonical materialization ALWAYS goes to `~/.lionagi/runs/{run_id}/artifacts/` (reusing the `_runs.py` RunDir precedent, so RunDetail can surface it). When `artifact_dir` is supplied, the engine writes the IDENTICAL tree there as well — kdev's clean, run-scoped harvest mirror, so kdev never globs into `~/.lionagi`.
- Layout (both locations identical):

```text
{artifact_dir-or-run-dir}/
  manifest.json
  {node_id}/
    result.md      # human-readable text (chat response text; engine summary text)
    result.json    # {node_id, kind, status, model, cwd, result, error, started_at, completed_at}
```

- `manifest.json` schema:

```json
{
  "run_id": "<session id>",
  "workflow": {"name": "...", "namespace": "...", "version": 3, "content_hash": "..."},
  "status": "completed | failed | cancelled",
  "base_dir": "/abs/path | null",
  "started_at": 1752000000.0,
  "completed_at": 1752000042.0,
  "nodes": [
    {
      "id": "analyze",
      "kind": "chat | engine",
      "status": "completed | failed | skipped",
      "model": "provider/model | null",
      "cwd": "/abs/path | null",
      "started_at": 1752000000.0,
      "completed_at": 1752000010.0,
      "artifacts": ["analyze/result.md", "analyze/result.json"],
      "error": null
    }
  ]
}
```

- For an ephemeral file-run (D-F3), the `"workflow"` object is `{"spec_hash": "...", "ephemeral": true}` instead of the registry tuple.
- **Materialization TIMING is engine-internal, not part of the contract kdev builds against** (kdev globs `{node_id}/result.*` and reads `manifest.json` regardless of when they were written). v1.1 materializes from `operation_results` at run end, in the teardown path (which runs in `finally`, so a flow that returns with per-node errors still materializes). The `artifacts_path` is recorded on the session row through the existing `_teardown_common(artifacts_path=…)` seam.
- Known limitation (documented, NOT a contract break to fix later): a hard SIGKILL/OOM, or an exception thrown out of `session.flow()` before it returns `operation_results`, loses that run's artifacts — the same failure class the existing `~/.lionagi/runs` layout already accepts. Per-node streaming materialization is a non-breaking v1.2 upgrade; it requires the `on_progress` callback (or a sibling completion hook) to carry the completed node's result payload, which it does NOT today (flow_signals.py:76). The harvest layout is unchanged by that upgrade.

### D-F3 — `li flow run <name|file>` surface

Synopsis:

```text
li flow run <name | name@version | file.json>
            [--input KEY=VALUE ...]
            [--base-dir PATH]
            [--artifact-dir PATH]
            [--json]
```

- **Name mode is the primary/production path.** `<name>` resolves through `definitions` (ADR-0102): a bare name = latest version in the `core` namespace; `name@version` pins a version. The run records `library_ref` + `library_content_hash` (ADR-0101/0102 columns), closing the run→library-version provenance gap. This is the mode kdev installs its pipeline into (via the ADR-0102 git-repo installer) and invokes.
- **File mode is an explicit ephemeral dev/CI convenience.** A path argument (exists on disk / `.json` suffix) is compiled and run WITHOUT persisting a `definitions` row. The run metadata records `spec_hash` (sha256 over the canonicalized spec) + a `spec_snapshot`; `workflow_def_id` and `library_ref` are `null`. RunDetail MUST render a run with a null `workflow_def_id` (verify-by gate below).
- Execution is **in-process** (the `run_workflow_def` library call), so `li flow run` works in a worktree/CI with no Studio daemon — the environment kdev actually runs in. Routing the call through the daemon when one is up is an optional future optimization, not required for v1.1.
- Prints `{"run_id": "...", "status": "..."}` (JSON with `--json`); artifacts land under `--artifact-dir` (and the canonical run dir) per D-F2.

### D-F5 — Security posture

- **Load-bearing invariant: `base_dir` is a run-level INPUT, never a field in the def spec.** The real threat, post-ADR-0102, is a SHARED/CONTRIBUTED def (authored in the workflows git repo, installed into `definitions`) carrying a hostile node cwd. Because containment is `node.cwd` under the operator-supplied `base_dir`, and the def cannot set `base_dir`, a shared def cannot escape the root the running operator chose. If a def could pin `base_dir`, containment would be meaningless.
- The engine performs its OWN cwd containment (`has_traversal` on the raw string + `relative_to(base_dir)` + must-exist + must-be-dir). It does NOT delegate to the CLI provider's check: `claude_code`'s repo-bound check only fires under `bypassPermissions` (line 392), so it is not a reliable containment for arbitrary provider permission modes.
- No cwd allowlist infrastructure in v1.1 — `base_dir`-per-run supplied by the operator already bounds execution, and an allowlist is config surface a single-operator tool does not need. `LIONAGI_FLOW_CWD_ALLOWLIST` is named as the v2/multi-operator (commercial) extension point; no tenant-isolation logic enters the OSS engine.

## Consequences

**Positive**

- kdev builds its full command + harvest surface against stable contracts (config field names, CLI synopsis, artifact layout, manifest schema) without waiting on implementation.
- #1922 is fixed with a hard, loud rule (provider-prefixed model) that eliminates a known silent-green class.
- Artifact contract reuses the existing `_runs` layout and `_teardown_common` seam rather than inventing parallel infra; runs remain visible in RunDetail.
- Provenance from a flow run back to its library version comes for free via ADR-0102 in name mode.
- Materialization timing can improve (per-node streaming) later without kdev churn — timing is not contract.

**Negative**

- v1.1 loses a run's artifacts on hard-kill or an exception thrown out of `session.flow()` (bounded, documented, fixable non-breakingly).
- A saved def with a bare `config.model` string now fails to run (clear compile error) instead of silently ignoring the model — a deliberate, one-time authoring correction.
- Dual name/file surface is slightly more CLI than a single mode, justified because kdev/CI needs the daemon-free file path and production needs the provenance-pinned name path.
- Session+flow overhead for a 3-node pipeline is real but acceptable: it is the same machinery every Studio run already uses, and it is what buys RunDetail visibility, persistence, and per-node signals for free.

## Alternatives Considered

| Fork | Alternative | Why rejected |
|------|-------------|--------------|
| F1 | (a) free-form absolute cwd per node, no containment | Route-reachable + shared-def surface makes an uncontained absolute cwd an arbitrary-dir subprocess vector. |
| F1 | (c) named workspace from a preceding sandbox node (engine-managed worktrees) | Real v2 direction, but adds a workspace-lifecycle primitive; kdev already creates and owns its worktrees, so v1.1 only needs cwd passthrough + containment. Named as the v2 extension point; the `base_dir` + `config.cwd` contract does not churn when it lands. |
| F2 | (b) explicit `output` node kind writing named files | Per-def authoring burden; every pipeline must wire output nodes. Rejected for the zero-authoring end-materialization. |
| F2 | (c) nodes write files themselves, convention-only | No deterministic layout/manifest for kdev to glob; loses the harvest contract. Rejected. |
| F2 | materialize at run end ONLY, in the happy path | Overturned in part: relocated into the teardown/`finally` path so a flow returning with per-node errors still materializes; end-only-on-success would drop partials on the common failed-but-returned case. |
| F3 | file mode = import-then-run (persists/updates a def row) | Pollutes the ADR-0102 registry of record with dev iterations; ephemeral + `spec_hash` preserves reproducibility without a row. |
| F3 | ephemeral file mode as the primary surface | In tension with ADR-0102's run→library provenance invariant; name mode (registry-resolved, provenance-pinned) is primary, file mode is the dev/CI convenience. |
| F4 | three-level inheritance node > def-default > branch | Overturned: no v1.1 consumer sets a def-level default (kdev sets model per node), and a `spec.default_model` field expands the schema + migration surface. Trimmed to two kind-specific levels. |
| F4 | accept bare strings + normalize with a default provider prefix at compile | Silently picks a provider — the exact trap being eliminated. Reject-at-compile instead. |
| F5 | cwd allowlist env (`LIONAGI_FLOW_CWD_ALLOWLIST`) in v1.1 | Config surface a single-operator tool does not need; `base_dir`-per-run already bounds it. Named as v2/commercial extension. |
| F5 | document-as-trusted-surface, validation only, no engine-level containment | Insufficient once ADR-0102 makes defs shareable: a contributed def could carry a hostile cwd. Engine-level containment under an operator-supplied `base_dir` is required. |
| Engine choice | option A (new standalone file runner) | Already rejected by Leo 2026-07-09 (build on the existing workflow-defs engine); recorded here for completeness. |

## Verify by

1. A chat node with `config.model = "claude/sonnet"` runs against Anthropic (not the branch default); a node with a bare `config.model = "sonnet"` fails compile with a node-scoped `WorkflowCompileError`. A def omitting `config.model` runs on the branch default unchanged.
2. A node whose resolved `config.cwd` escapes `base_dir` (via `..`, symlink, or absolute-outside) is rejected; a contained cwd runs the provider in that directory. A def cannot set `base_dir`.
3. `li flow run <name>` resolves through `definitions` and the run records `library_ref` + `content_hash`; `li flow run spec.json` runs ephemerally with `spec_hash` recorded and RunDetail renders with `workflow_def_id = null`.
4. After a run, `manifest.json` + `{node_id}/result.{md,json}` exist under both the run dir and (if given) `artifact_dir`, with per-node status matching the run outcome.
5. **Concurrency gate (blocking for the `li flow run` slice):** an off-daemon `li flow run` executed WHILE the Studio daemon is running a concurrent flow completes with no `"database is locked"` error. Confirm `busy_timeout` is installed before any lock acquisition on the request-scoped connection (the documented cause of the prior StateDB WAL promotion deadlock — this is a regression check, not new design).
6. Existing saved workflow defs still compile/run (or fail with a clear node/edge-scoped `WorkflowCompileError`); no silent behavior change. Schema parity set updated if any column is added.

## Spikes

No fork requires a blocking design spike before ruling. Two items are empirical VERIFY gates (above), not spikes: the StateDB WAL concurrency check (#5) and the RunDetail null-`workflow_def_id` render (#3). The one deferred enhancement with a named enabler — per-node streaming materialization requiring `on_progress` to carry the node result payload — is explicitly OUT of v1.1 scope and does not gate it.

## REFUTATIONS (leanings overturned or refined)

- **F1 lean "run-level base_dir + node-RELATIVE subpaths" → refined.** Kept `base_dir` containment but allow node cwd to be ABSOLUTE (contained via `claude_code`'s `relative_to` check), so kdev passes `git worktree add` paths directly. Added the security-load-bearing rule the lean omitted: `base_dir` is a run INPUT, never a spec field.
- **F2 lean "materialize at run end" → partially overturned + reframed.** Timing is NOT the kdev contract (layout is), so it must not be over-specified. Materialization moved into the teardown/`finally` path (survives failed-but-returned runs); the thrown-exception/SIGKILL partial-loss gap is documented and named as a non-breaking v1.2 fix that needs an `on_progress` result-payload extension the current seam lacks (verified flow_signals.py:76). Also overturned "artifact_dir is THE location": canonical is the run dir, `artifact_dir` is an optional mirror wired through the existing `_teardown_common` seam.
- **F3 lean "file = ephemeral, dual-mode" → refined against ADR-0102.** Ephemeral file mode kept, but demoted to a dev/CI convenience; NAME mode (registry-resolved, provenance-pinned per the just-accepted ADR-0102) is the primary path. Overturned the packet's implicit "off-daemon always" as the sole framing: in-process is right for kdev (no daemon), daemon-routing is an optional later optimization.
- **F4 lean "three-level inheritance" → overturned to two levels.** No v1.1 consumer needs a def-level default; a `spec.default_model` field is deferred. Also corrected the stated failure mode: a bare chat model string does not become `provider=<modelname>` — it falls back to `LIONAGI_CHAT_PROVIDER` (imodel.py:70-73). The reject-at-compile ruling stands; the packet's rationale was imprecise. Confirmed migration safety: since `config.model` was never applied for chat, rejecting bare strings breaks no working behavior.
- **F5 lean "claude_code-style containment, validation-only, no allowlist" → hardened, not softened.** The lean is right on mechanism but under-justified. The real threat is ADR-0102 shared/contributed defs, not operator-vs-operator; the load-bearing defense is `base_dir`-is-a-run-input (a def cannot pin its own root), plus engine-level containment that does NOT delegate to `claude_code` (whose check only fires under `bypassPermissions`, line 392).
- **Engine-choice residual risk (packet refute mandate).** Session/flow overhead for a 3-node pipeline and StateDB coupling for a CLI-first consumer were attacked and SURVIVE: the overhead buys RunDetail/persistence/signals every Studio run already pays for, and the CLI's in-process request-scoped StateDB connection is already designed for daemon-concurrency (workflow_run docstring). The one residual is verified empirically by gate #5, not assumed.

## References

- ADR-0102 (workflow library registry); ADR-0101 (task application queue, `library_ref`/`library_content_hash` columns).
- Prior advisory "Studio WorkflowDef compile bridge" (option-B ruling; entity 61adfc76).
- Issue #1922 (per-node model not applied).
- Source: `lionagi/studio/services/workflow_compile.py`, `workflow_run.py`, `workflow_defs.py`; `lionagi/service/imodel.py`; `lionagi/providers/anthropic/claude_code.py`; `lionagi/state/db.py`; `lionagi/engines/flow_signals.py`; `lionagi/cli/_runs.py`.
