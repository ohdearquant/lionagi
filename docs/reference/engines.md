# Engine Reference

Parameter catalogs for the engine classes in `lionagi.engines`.
All engines inherit from `Engine` (base parameters documented first).

## Engine (base)

Stateless event-driven engine base.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `model` | `None` | Default model string for every stage. |
| `models` | `None` | Optional per-stage model overrides: `{"stage": "provider/model", ...}`. The constructor copies a provided mapping and normalizes `None` or an empty mapping to an internal `{}`. |
| `max_depth` | `3` | Maximum recursion depth or cycle generation. |
| `max_concurrent` | `5` | Concurrency semaphore width (simultaneous agent slots). |
| `max_agents` | `50` | Hard cap on total agents the run may create. |
| `deadline_s` | `None` | Wall-clock cap in seconds; `None` = unbounded. |
| `judge_model` | `None` | If set, a cheap gate agent runs at expansion points. `None` = no gate (fail-open). |
| `judge_role` | `"critic"` | Casts role for the judge agent. |
| `cancel_timeout_s` | `30.0` | Seconds to wait for spawned tasks to settle after cancellation. Tasks still pending at the deadline are warned about and abandoned after one final cancellation request. |

On budget exhaustion `Engine.run()` calls `_partial_export()` (override in subclasses to return a partial result) instead of raising. Caller-initiated cancellation still propagates as `CancelledError`.

---

## CodingEngine

Gated implement/test/fix-loop engine (`lionagi.engines.coding`).

Extends `Engine` with:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `plan_role` | `"analyst"` | Casts role that writes the implementation plan (`WorkPlanned`). |
| `implement_role` | `"implementer"` | Casts role that writes code using `coding_tools`. |
| `verify_role` | `"critic"` | Casts role that issues the `VerifyResult` verdict. |
| `coding_tools` | `("coding",)` | Tool names granted to the implementer agent. |
| `implement_permissions` | `"safe"` | Permission preset for the implementer. |
| `max_fix_rounds` | `3` | Maximum test-fail re-prompt rounds before concluding. |
| `test_timeout_s` | `600.0` | Wall-clock cap for each subprocess test run. |
| `repair_retries` | `1` | Emission-repair turns per stage when the model emits no valid event. |
| `turn_timeout_s` | `600.0` | Wall-clock cap for each implementer model turn, including fix turns. A timeout records `turn_timeout` and drives the emission-repair path; `None` disables the cap. |
| `strict_spec` | `False` | Raise `ValueError` on the first spec-lint warning before creating an agent. When `False`, warnings are emitted and execution continues. |
| `heartbeat_interval_s` | `30.0` | Seconds between implement-stage `WorkerHeartbeat` checks; file mtime changes also emit `WorkerActivity`. `None` disables heartbeats. |
| `stage_timeout_s` | `None` | Optional wall-clock cap applied to every model stage. A timeout always emits `WorkAborted` (with a `hard` flag). Implement and fix timeouts are hard: the run aborts and concludes failed. Plan and verify timeouts are soft: the stage is bounded but the run recovers — a timed-out planner degrades to the raw task text, and a timed-out verifier omits its advisory verdict while the test result still decides pass/fail. |
| `worker_extra_tools` | `()` | Additional tool names granted only to the implementer, appended to `coding_tools`. |
| `worker_mcp_servers` | `None` | Optional MCP server names granted only to the implementer. |
| `worker_extra_prompt` | `None` | Optional extra prompt text passed only to the implementer. |
| `auto_repair_cmds` | `None` | Optional commands run in sequence before each authoritative full test gate. Successful commands emit `AutoRepairApplied`; failed commands are reported without aborting the run. Internally normalized to `[]`. |
| `fast_test_cmd` | `None` | Optional string or argument-list command used as an incremental gate on intermediate substantive fix rounds; the declared `test_cmd` remains the final ground-truth gate. |

Per-stage model routing keys: `"plan"`, `"implement"`, `"verify"`.

Pipeline shape: plan → implement → test → [fix loop, up to `max_fix_rounds`] → verify → `CodeResultRecorded`.

---

## HypothesisEngine

Evidence-chain engine for hypothesis-driven development (`lionagi.engines.hypothesis`).

Extends `Engine` with one casts role per stage:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `question_role` | `"analyst"` | Extracts `QuestionRaised` events from findings. |
| `research_role` | `"researcher"` | Gathers `EvidenceCollected` for a question. |
| `hypothesis_role` | `"analyst"` | Forms `HypothesisFormed` or direct `ConclusionDrawn` from evidence. |
| `design_role` | `"evaluator"` | Designs the decisive `ExperimentDesigned`. |
| `validate_role` | `"analyst"` | Executes experiments and records `ResultRecorded`. |
| `conclude_role` | `"critic"` | Draws `ConclusionDrawn` from a result. |
| `apply_role` | `"architect"` | Maps conclusions onto decisions as `ApplicationMapped`. |
| `synthesis_role` | `"synthesizer"` | Writes the final evidence report. |
| `executable_methods` | `("analysis", "comparison", "proof")` | Experiment methods the validator runs inline. Others queue on `run.pending`. |
| `validate_tools` | `()` | Tool names granted to the validator agent for real measurements. |
| `validate_cwd` | `None` | Workspace the validator is path-guarded to. |
| `validate_permissions` | `"safe"` | Permission preset for the validator. |
| `max_questions` | `8` | Per-extraction cap threaded into the prompt (soft; judge and budget are the hard bounds). |
| `repair_retries` | `1` | Re-prompt turns when an expected emission did not arrive. |

`max_depth` (from `Engine`) bounds back-edge cycles: follow-up questions and findings raised at `gen > max_depth` are recorded but not expanded.

Per-stage model routing keys: `"extract"`, `"research"`, `"hypothesize"`, `"design"`, `"validate"`, `"conclude"`, `"apply"`, `"synthesize"`.

Pipeline shape: seed findings → extract questions → gather evidence → form hypotheses → design experiments → validate → draw conclusions → apply to decisions → synthesize report.

---

## PlanningEngine

Plan-then-execute engine over the reactive DAG executor (`lionagi.engines.planning`).

This is the engine `li o flow` uses as its CLI front-end.

Extends `Engine` with:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `orchestrator_role` | `"orchestrator"` | Casts role that decomposes the prompt into a `list[TaskAssignment]`. |
| `roles` | `("researcher", "analyst", "critic", "architect", "synthesizer")` | Roster the orchestrator may assign workers to. |
| `synthesis_role` | `"synthesizer"` | Casts role that writes the final deliverable from worker outputs. |
| `reactive` | `True` | When `True`, every worker is granted `SpawnRequest` so the live DAG self-expands. `False` runs a flat, fully-planned DAG. |

Pipeline shape: orchestrate (produce DAG) → execute reactively → synthesize outputs into deliverable.

---

## ResearchEngine

Recursive, reaction-driven research engine (`lionagi.engines.research`).

Extends `Engine` with:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `novelty_threshold` | `0.7` | A `FindingEmitted` above this novelty spawns a deeper exploration node. |
| `roles` | `("researcher", "analyst", "critic")` | Casts roles forming each exploration team (run in sequence, sharing output). Each is granted the research emissions. |
| `synthesis_role` | `"synthesizer"` | Casts role that writes the final synthesis from the emission store. |
| `repair_retries` | `1` | Re-prompt turns when an exploration node's whole team emitted no finding. |

`max_depth` (from `Engine`) bounds recursive depth; `max_agents` and `deadline_s` bound cost.

Pipeline shape: explore topic (team of agents) → spawn deeper nodes on high-novelty findings → quiesce → synthesize.

---

## ReviewEngine

Dimensional review engine (`lionagi.engines.review`).

Extends `Engine` with:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `dimensions` | `("correctness", "security", "performance", "maintainability")` | Review lenses; each runs one reviewer agent in parallel. |
| `reviewer_role` | `"critic"` | Casts role for dimension reviewers. |
| `verifier_role` | `"critic"` | Casts role for adversarial verifiers. |
| `synthesis_role` | `"synthesizer"` | Casts role that issues the terminal `ReviewVerdict`. |
| `verify_severities` | `("critical", "major")` | Issue severities that reactively spawn an adversarial verifier. |
| `repair_retries` | `1` | Re-prompt turns when a reviewer or verifier emits no valid event. |

Pipeline shape: fan-out (one reviewer per dimension, parallel) → adversarial verify high-severity issues → quiesce → `ReviewVerdict`.

Every dimension emits affirmatively: issues arrive as `IssueFound`, a clean dimension arrives as `DimensionClean` (dimension + one-sentence rationale). A dimension that emits nothing is therefore a transport failure, never an implicit all-clear — the verdict instruction lists affirmed-clean dimensions separately so downstream consumers can distinguish "reviewed, clean" from "reviewer never reported". Verifier arrival keys on a short engine-assigned `ref` token echoed back in the `VerifyResult`, so a paraphrased issue description does not burn repair rounds.
