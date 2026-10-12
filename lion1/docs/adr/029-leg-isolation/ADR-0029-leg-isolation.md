---
adr: ADR-0029
status: proposed
liveness: unimplemented
date: "2026-10-10"
area: leg-isolation
kind: new
depends_on:
  - "[[ADR-0011-the-box|ADR-0011]]"
  - "[[ADR-0013-the-agent|ADR-0013]]"
  - "[[ADR-0016-the-lion-command|ADR-0016]]"
  - "[[ADR-0025-the-graph|ADR-0025]]"
  - "[[ADR-0026-the-submission|ADR-0026]]"
tags:
  - adr
  - runtime
  - isolation
---

# ADR-0029: Worker isolation

## Context

The runtime needs an isolated execution mode for submitted tasks. Today `cast` uses one directory
for the actor, store client, backend and tools. A VM contains the shell but host readers share its
writable mount, and its offline network still reaches host services. Naming another actor changes
attribution; it does not remove these routes.

This record separates trusted control state from task files and admits only measured boundaries. It
covers worker access, report export and cleanup, not a resident's wake policy. ADR-0013 C9 and D7
remain scoped to residents. The host runtime, store client, backend launcher and VM platform are
trusted; arbitrary task programs and model-directed operations are not.

Source: `submit.py:cast/run_main`, `chat.py:console/signer` in `apps/cli/lion_cli/`;
`hub/tools/shell.py`, `code.py`, `fs.py`, `khive.py`, `runta.py`;
`hub/backend/claude_code/__init__.py`, `hub/backend/codex/__init__.py`; `src/lion1/khive.py`.

## Definitions

- **worker actor**: the fresh attributed namespace for one admitted task run, with no authority
  inherited from its name or submitter.
- **control directory**: supervisor-owned private configuration, databases, blobs and lifecycle
  state, inaccessible through the worker's tools or mounts.
- **task stage**: the per-run copy of declared task inputs and writable scratch seen by the guest's
  runtime tools, with no control state.
- **isolation manifest**: the immutable admission record binding identity, input bytes, paths,
  policies, limits and qualified software to a run.
- **qualification receipt**: recorded positive controls and attempted forbidden operations from the
  worker's routes, tied to the exact environment tested.
- **export receipt**: a supervisor record of an artifact's relative name, byte length, digest and
  publication status, independent of the model's reported result.

## Assumptions

| id | statement | status | evidence and falsifier |
| --- | --- | --- | --- |
| A1 | A config-less lion1 run refuses before inference. | code-checked | `signer`, `_submitter`, `Khive.__post_init__`; falsified by a launch that calls a model without an attributed config. |
| A2 | Today's offline VM can reach the host. | code-checked; prior measurement | `Sandbox.host_only/start`; ADR-0011 D4/S20 reports host reach. No live probe was repeated here. |
| A3 | A tools-off backend can run on the host without model-directed host access or ambient instruction loading. | assumed | Claude `_exec` and Codex `_flags/_catalog` are partial mechanisms. Falsified by a canary read, hidden tool, hook or unapproved instruction in D5 qualification. |
| A4 | The local VM adapter can deny all external guest traffic before task code runs. | assumed | Current `offline` does not suffice. Falsified by any host, LAN, DNS, metadata or guest-peer response under D7. |
| A5 | One prepared image and declared input set suffice for the selected task scope. | assumed | Falsified by paired tasks needing unstaged dependencies, live services or another host environment. Refuse or narrow the scope. |
| A6 | The supervisor can retain and retrieve a run without granting the worker store access. | assumed | `Khive` accepts explicit config; result authorization and private storage remain owed. Falsified by failed owner read-back or sibling access in D1. |
| A7 | Host administrators, the pinned backend executable and VM platform are trusted. | assumed | A malicious host process or platform compromise invalidates this threat model. This record makes no defense against either. |

## Claims

### C1: Admission binds one isolated run _(enforced: mechanical; owed, S1)_ ^c1

- **Subject**: every submission admitted with worker isolation.
- **Violated when**: inference begins without a frozen isolation manifest, or a replay opens a
  second run for the same admission key.

The supervisor authenticates the submitter, allocates a run id and worker actor, and checks the
result recipient against the submitter's authority. A caller-supplied actor string is not
authentication. The isolation manifest binds actor, submitter, recipient, control directory, task
stage, input and profile hashes, tool set, network policy, binary digests, qualification receipt and
finite limits. The row exists before inference. Mutable source files, symlinks, environment or
profile reloads cannot widen it; conflicting reuse of its admission key refuses.

### C2: Control state stays private _(enforced: mechanical; owed, S2)_ ^c2

- **Subject**: each worker's config, persistence, launch inputs and result reads.
- **Violated when**: a task path selects identity, a worker reaches control state, or another run
  reads its records.

The control directory holds a private main store, separate lion database, blob root and daemon
socket. None routes to the shared store or another run. Only the supervisor's store client receives
its frozen absolute config and selected binary, asserting the worker actor on every call.

The authenticated submitter and authorized supervisor/operators retrieve records through the
supervisor; the worker receives no such route. Workspace and ambient configs, hooks, plugins,
instruction discovery and credential variables supply no launch authority. Explicitly admitted
instruction bytes are hashed inputs. Empty or inconsistent actor configuration refuses.

### C3: Inputs become a closed task stage _(enforced: mechanical; owed, S3)_ ^c3

- **Subject**: staging and every mount given to a worker.
- **Violated when**: staging reads an undeclared target, mounts a caller's live tree, or includes
  host control paths, sockets or cross-run writable storage.

Admission snapshots declared regular input files into the task stage. It refuses special files,
external links and path races; internal links are safely materialized or refused. It records the
bytes actually copied and never follows a worktree's `.git` pointer. Git metadata, credentials and
runtime configuration are excluded from implicit staging, including tracked copies.

Task source needed as config test data requires explicit declaration and remains inert to the
launcher. A fresh guest repository may be built from the staged bytes. The guest gets private
scratch, home, temporary space and writable dependency storage; immutable prepared dependencies may
be shared read-only. The original checkout and other runs cannot be changed.

### C4: Readers and shell use one guest tree _(enforced: mechanical; owed, S4)_ ^c4

- **Subject**: every model-directed file read, listing, search, edit and shell call.
- **Violated when**: a handler operates on a host task path, readers lag behind shell writes, or a
  path race escapes the file tools' task stage.

All file handlers use one guest tree; all shell commands use that guest's task stage and scratch.
File tools confine traversal at use, including searches and concurrent symlink changes, rather than
checking a path then opening it by name. Shell programs may read the guest image, which contains no
host credentials or control state. No host home, runtime socket or shared writable cache is mounted.
The `read` grant excludes `edit`; a separately granted shell may write staged files.

Task processes run unprivileged in a supervisor-owned cgroup; immutable tool helpers and management
interfaces are inaccessible to them.

### C5: Host inference grants no host tools _(enforced: mechanical; owed, S5)_ ^c5

- **Subject**: initial, resumed and retried CLI backend calls of an isolated task.
- **Violated when**: a backend exposes its own tool, loads ambient instructions or executable
  configuration, or runs an unqualified release.

The backend may run on the host as a trusted inference transport. It receives a clean per-run cwd,
an isolated auth-only configuration home and an allowlisted environment. It loads no task hooks, MCP
servers, plugins or home/project instructions beyond the admitted prompt. Account credentials stay
on the host and are never forwarded to the guest.

Qualification covers effective tools and prompt inputs, not permission flags alone. CLI version,
executable, tool-disabling flags and catalog policy are bound to the qualification receipt. An
unknown combination refuses before inference; production cannot set an unmeasured bypass.

### C6: Worker store reach is absent _(enforced: mechanical; owed, S6)_ ^c6

- **Subject**: tools, automatic context sources and network routes available to a worker.
- **Violated when**: model input invokes a store client or mailbox, or store context is injected
  without being part of the declared input set.

The initial task profile grants no khive tool, MCP server, `send`, mailbox verb, store-backed note
command or experience lookup. Defaults are filtered as well as explicit profile tools. Unknown
commands refuse at dispatch. The worker cannot choose a config, actor, namespace, socket or result
recipient.

The runtime still records its own messages, calls and outcomes through its private store client.
This persistence supplies no general operation endpoint to the model. Terminal delivery is a
supervisor action to the frozen recipient, never a worker mailbox capability. Admitted input bytes
may contain prior context; they confer no live store route.

### C7: Guest networking starts closed _(enforced: mechanical; owed, S7)_ ^c7

- **Subject**: network traffic of every guest and its descendants, from creation to teardown.
- **Violated when**: task code reaches anything outside guest loopback, or closure is installed only
  after task code has begun.

The task network policy denies guest DNS, internet, host gateway, host LAN addresses, metadata
services and other guests for IPv4 and IPv6. Enforcement is outside the task's privilege and cannot
be changed by its shell. The current host-only `offline` setting is insufficient. Admission refuses
an adapter that cannot establish the policy before any task program starts and prove it from inside
the guest.

Host inference uses its selected provider/account route; it is neither guest egress nor a guest
proxy. Dependencies are prepared before admission. No silent retry opens networking.

### C8: Export publishes bounded reports _(enforced: mechanical; owed, S8)_ ^c8

- **Subject**: every report exported from the task stage.
- **Violated when**: export follows a link, publishes unchecked bytes, exceeds its bounds, or
  chooses a host destination from model text.

The supervisor exports only declared relative report paths after every task cgroup is stopped. Only
the trusted export helper may then execute; task writers cannot race the extraction. Each must be a
regular UTF-8 file: at most four files, 1 MiB per file and 4 MiB total. Missing, linked, special,
changed or oversized files fail explicitly; nothing is silently truncated.

Guest-side safe opens and a bounded byte stream yield an export receipt. The supervisor verifies
size and digest before atomic publication under its run-owned artifact directory. It never resolves
a guest link on the host or applies a patch. Store output holds bounded receipt metadata, not the
report body; metadata exceeding 16 KiB fails before the inline store call.

### C9: Run outcome and handoff stay distinct _(enforced: mechanical; owed, S9)_ ^c9

- **Subject**: durable job state, `result`, exit status and terminal delivery.
- **Violated when**: export or mail failure rewrites a successful run, a dead worker implies
  success, or a retry reruns inference merely to deliver an existing result.

Persist run outcome separately from export, delivery and cleanup states, each with pending, success
or failure and its error; unknown execution remains unknown. `result` shows the complete state and
available receipts. A job is complete and exits 0 only when the run succeeded and every required
phase succeeded. A report failure can coexist with run success and a nonzero job exit.

Persist terminal delivery intent before sending, using one stable idempotency key and the frozen
recipient. A lost acknowledgment is retried with the same key. Export and delivery retry from
retained evidence, never by replaying the model's work.

### C10: Qualification uses attempted forbidden reads _(enforced: mechanical; owed, S10)_ ^c10

- **Subject**: admission of any isolation adapter, backend combination and concrete guest.
- **Violated when**: submit-time fields, a model assertion or an unexercised path count as a passing
  qualification receipt.

A synthetic user store holds a unique mailbox canary outside the guest. A trusted positive control
proves it readable and the target service live. Runtime file tools, shell and backend tool routes
attempt the forbidden reads. The harness records attempted operations, actual refusals and canary
access observations. Missing attempts or failed controls are inconclusive and refuse admission.

Cover traversal, symlink races, alternate configs, MCP, credential discovery, host-network routes,
sibling storage, report export and teardown. A receipt binds runtime revision, host and VM versions,
image digest, backend executable, selected model, launch configuration and suite revision. Every
instance repeats deterministic boundary checks; any material tuple change requires fresh live
qualification.

### C11: Teardown revokes execution _(enforced: mechanical; owed, S11)_ ^c11

- **Subject**: every success, refusal, timeout, crash and cancellation after resources are reserved.
- **Violated when**: surviving processes retain an access route, a resource is forgotten after a
  failed delete, or a live writable cache is reused.

The supervisor records backend groups, guest identifiers, volumes, sockets and temporary paths
before launch can leave them behind. It stops every writer, completes bounded export or records its
failure, then removes execution resources. A supervisor restart reconciles these identifiers; it
neither trusts a recycled pid nor reruns ambiguous work.

Failed export may retain a sealed artifact snapshot under the admitted policy, never an authorized
live worker. Failed teardown preserves resource identifiers and an error, triggers retry, and blocks
reuse. Qualification finishes only after its canary resources and deliberately orphaned children are
gone; a second interrupt cannot erase the cleanup obligation.

### C12: Resource and retention limits are admitted _(enforced: mechanical; owed, S12)_ ^c12

- **Subject**: each task's resource reservation, execution deadline and retained evidence.
- **Violated when**: a run starts without finite limits, a deadline leaves execution running, or
  retention silently consumes another run's reservation.

Admission requires finite wall time, guest CPU and memory, scratch and record bytes, concurrent-run
capacity, cleanup deadline, and retention duration and bytes. It reserves capacity before starting
and refuses when unavailable. The supervisor enforces wall time across backend retries and tool
calls, including code blocked outside the runtime's turn loop.

Quota exhaustion records failure; it never presents a truncated report or incomplete row as whole.
Retention preserves identity, manifest, run state and export receipts for authorized read-back, then
purges owned data and records that deletion. It grants no worker mailbox. Dollar accounting follows
ADR-0003 C5/D5; unknown cost is not zero or a substitute for a wall deadline.

## Decisions

### D1: Admit a fresh actor and immutable manifest ^d1

Serves C1 and C2. Add `--isolation leg --workspace W` beside `--dir D`, the trusted submitting
user's directory. Admission resolves the profile there, creates an opaque `leg:<uuid>` and private
control directory, and persists the submitter relation. No graph actor entity is required. Bind
result access to the authenticated submitter and supervisor/operator policy; never derive it from an
actor-name prefix. Refuse conflicting duplicate admissions.

- **Landing evidence**: owed (S1): `test_leg_admission_is_idempotent`,
  `test_leg_recipient_requires_authority`, `test_leg_owner_reads_retained_result`,
  `test_sibling_leg_cannot_read_records` in `apps/cli/tests/test_leg_admission.py`.

### D2: Give builders two paths and closed launch inputs ^d2

Serves C1 and C2. `cast(actor_dir, workspace, ...)` and `console(workspace, actor_dir=..., ...)`
take distinct paths in isolated mode. The private `Khive` uses `actor_dir` as cwd and its frozen
absolute config. Select the binary using ADR-0025 D13, then pin its resolved path and digest.
Disable ambient hook and instruction discovery for isolated runs; explicit admitted prompt bytes
replace it. Keep generic console behavior separately scoped.

- **Landing evidence**: owed (S2): `test_leg_paths_are_disjoint`,
  `test_leg_ignores_hostile_workspace_config`, `test_leg_launch_uses_frozen_inputs`,
  `test_leg_storage_is_private` in `apps/cli/tests/test_leg_admission.py`.

### D3: Copy inputs, not host state ^d3

Serves C3. Create the task stage from a declared input manifest using race-safe file handles.
Exclude live git metadata, sockets, credentials and implicit runtime configuration; preserve
excluded source only through explicit inert input declarations. Refuse ambiguous links. Give each
guest private writable storage, including `/opt`; prepared dependencies belong to a pinned read-only
image. Record the staged bytes and omissions so qualification compares the same inputs.

- **Landing evidence**: owed (S3): `test_leg_stage_rejects_links_and_devices`,
  `test_leg_stage_rejects_copy_races`, `test_leg_stage_excludes_control_files`,
  `test_leg_writes_leave_source_and_siblings_unchanged` in `tests/test_leg_stage.py`.

### D4: One local VM for runtime tools ^d4

Serves C3 and C4. Use the `Tree` interface through a qualified guest tree and `Sandbox` for the
first supported mode. Reuse file and code handler semantics, including ADR-0011 D6's read grant. Add
safe in-guest traversal and search; current resolve-then-open behavior is insufficient under
mutation. Do not run host `LocalTree` over a guest-writable mount. No remote adapter is admitted by
class membership alone.

- **Landing evidence**: owed (S4): `test_leg_readers_observe_shell_writes`,
  `test_leg_reader_search_resists_symlink_races`, `test_leg_read_grant_has_no_edit`,
  `test_leg_mounts_have_no_shared_writable_state` in `tests/test_leg_tools.py`.

### D5: Qualify clean host backend launches ^d5

Serves C5. A backend launch policy selects an absolute executable, isolated auth-only home, clean
cwd and allowlisted environment. Empty Claude tool/MCP configuration and Codex's filtered catalog
are necessary inputs, not proof. Qualify initial calls, resumes and retries on each selected model
and release. Refuse unknown combinations without production overrides. Backend-created sessions are
per run; no user session is resumed.

- **Landing evidence**: owed (S5): `test_leg_backend_launch_is_clean`,
  `test_leg_backend_rejects_unknown_release`, `test_leg_backend_resume_keeps_policy`,
  `test_live_leg_backend_cannot_read_canary` in `tests/test_leg_backend.py`.

### D6: No live store capability in the initial implementation ^d6

Serves C6. The isolated profile admits only required runtime control and task tools. Filter default
handlers and context providers as well as explicit grants; refuse khive, send, mailbox, notes and
experience access. Preserve the runtime's internal persistence client. A later typed gateway needs
its own corpus authorization, quotas and qualification; neither arbitrary ops nor inherited user
visibility is an acceptable first implementation.

- **Landing evidence**: owed (S6): `test_leg_default_handlers_have_no_store_route`,
  `test_leg_refuses_mailbox_and_alternate_configs`, `test_leg_injects_no_store_context`,
  `test_leg_persists_without_worker_store_access` in `tests/test_leg_store_boundary.py`.

### D7: Deny guest external networking before execution ^d7

Serves C7. Add a task network policy distinct from `offline`. The local adapter must enforce denial
outside guest privilege before startup; do not assert that a currently available container flag
already does it. Verify reach from the guest, including IPv6, host gateways and proxies. Failure or
lack of enforcement refuses this adapter. No fallback switches to open networking or a paid remote
service. The host backend retains its separate inference route.

- **Landing evidence**: owed (S7): `test_leg_network_is_closed_before_exec`,
  `test_live_leg_denies_host_dns_lan_and_metadata`, `test_leg_cannot_reconfigure_network` in
  `tests/test_leg_network.py`.

### D8: Supervisor copy-out with receipts ^d8

Serves C8. The submitter declares report paths at admission. The worker writes reports inside the
task stage; after quiescence a guest-side safe reader streams bounded bytes to supervisor-owned
temporary files. Verify digest, size and UTF-8 before atomic publication and durable export
receipts. Partial publication remains named and retryable; require every declared report for export
success. No worker export verb or report body in `run.output` is needed.

- **Landing evidence**: owed (S8): `test_leg_export_checks_type_size_and_digest`,
  `test_leg_export_rejects_link_swap`, `test_leg_export_is_atomic_and_retryable`,
  `test_leg_output_contains_receipts_only` in `tests/test_leg_export.py`.

### D9: Persist phase outcomes and delivery intent ^d9

Serves C9. Extend job supervision and `result` with separate run, export, delivery and cleanup
states. Preserve ADR-0026's nonzero exit on failed handoff while reporting successful computation
accurately. Persist delivery intent and one idempotency key before sending; retry after ambiguous
acknowledgment. The envelope names export failures even when no report is available. A durable job
record, not a process exit or model statement, owns terminal truth.

- **Landing evidence**: owed (S9): `test_leg_success_survives_export_failure`,
  `test_leg_delivery_retries_without_rerun`, `test_leg_lost_ack_reuses_key`,
  `test_leg_dead_worker_is_not_success` in `apps/cli/tests/test_leg_lifecycle.py`.

### D10: Version the adversarial probe suite ^d10

Serves C5, C7 and C10. Commit deterministic handler and policy tests plus an opt-in live suite. Use
a synthetic user-mailbox canary with positive controls. Replay attempts through the actual worker
routes; backend challenges also seek hidden native tools. Observe accesses outside the model
transcript. Bind receipts to the complete environment and reject missing coverage. Admission
requires a matching live receipt and passing per-instance deterministic preflight, before task input
reaches inference.

- **Landing evidence**: owed (S10): `test_leg_probe_requires_positive_controls`,
  `test_leg_probe_rejects_missing_attempts`, `test_leg_receipt_invalidates_on_change`,
  `test_live_leg_forbidden_read_matrix` in `tests/test_leg_qualification.py`.

### D11: Reconcile resources after every end ^d11

Serves C9 and C11. The supervisor owns resource registration, process-group cancellation, guest
quiescence, export and deletion. Retain bounded sealed evidence on export failure; revoke execution
first. Keep failed-deletion identifiers for restart reconciliation and prevent their reuse. Add a
janitor for orphaned reservations and resources; verify process identity before signaling. Cleanup
has its own outcome and deadline, independent of notification or the worker's final text.

- **Landing evidence**: owed (S11): `test_leg_teardown_kills_backend_and_guest_children`,
  `test_leg_crash_reconciliation_preserves_resource_ids`, `test_leg_delete_failure_blocks_reuse`,
  `test_leg_probe_teardown_removes_canary_resources` in `tests/test_leg_cleanup.py`.

### D12: Reserve finite resources and retained evidence ^d12

Serves C1, C11 and C12. The task submitter supplies finite quotas and retention; no implicit
unlimited value is admitted. Reserve them before resources are launched. Enforce the absolute wall
deadline outside the turn loop and across retries. Stop on scratch or record exhaustion and preserve
the error. Purge only this run's owned data when retention expires; record the deletion and make
result readers distinguish expired evidence from an unknown run.

- **Landing evidence**: owed (S12): `test_leg_requires_finite_reserved_limits`,
  `test_leg_wall_deadline_covers_blocked_calls`, `test_leg_quota_failure_is_not_success`,
  `test_leg_retention_purges_only_owned_data` in `tests/test_leg_limits.py`.

## Alternatives

| approach | rejected because |
| --- | --- |
| Reusable user-suffixed actor | shares identity across concurrent runs; separate storage is still needed and the suffix grants no safe ACL. |
| Anonymous worker | current console and store client require attributed config; anonymity also loses durable ownership. |
| Explicit config without a directory split | leaves cwd, instruction discovery, hooks and mounted control files coupled. |
| Host readers over a writable guest mount | current path checks and subprocess search are vulnerable to concurrent path changes. |
| Immutable host readers plus guest scratch | cheaper for source-only tasks, but readers cannot see generated files; qualifies only a separately declared narrow task scope. |
| Inference CLI inside the task VM | adds account plumbing and provider networking to the guest; unnecessary if the host launch passes tools-off qualification. |
| Current offline flag | blocks guest internet while retaining host-service access. |
| Gateway or full khive tool | adds corpus authorization and mailbox exclusion work; no live store access is required for the selected scope. |
| Worker-selected export destination | turns result publication into a host write capability and complicates safe path handling. |
| Exit status as terminal truth | loses the difference between computation, artifact publication, delivery and cleanup. |
| Daytona or Runta first | adds remote data placement, service credentials and resource approval; existing policies still need inside-box qualification. |

## Consequences

- **S1**: C1 and D1 are owed. Admission and result authorization are required before isolated
  submission is available. Actor allocation is attribution; physical store separation and route
  denial provide the initial access boundary.
- **S2**: C2 and D2 are owed. ADR-0026 gains separate submitter, worker actor and workspace terms.
  ADR-0025 keeps binary selection and database routing; this record adds per-run private storage.
- **S3**: C3 and D3 are owed. Staging consumes time and disk. Qualification records staged hashes,
  image, dependency inventory, omissions and staging duration; it compares matched input sets.
- **S4**: C4 and D4 are owed. Guest readers add transport overhead and require safe traversal.
  Measure reading, search and shell latency; existing host timings are not forecasts for this mode.
- **S5**: C5 and D5 are owed. A host backend remains trusted code holding account access. Live
  probes establish evidence for a fixed configuration, not safety against malicious CLI binaries or
  every future model behavior.
- **S6**: C6 and D6 are owed. Injection-enabled profiles need the explicit injection-off override of
  ADR-0026 C10/D8 for both task profiles. Full injection parity and a store gateway are outside the
  initial implementation.
- **S7**: C7 and D7 are owed. No supplied code establishes deny-all local networking. If it cannot
  be enforced and probed, the local shell mode remains unavailable; a remote service is an operator
  resource decision, not an automatic fallback.
- **S8**: C8 and D8 are owed. Report limits bound export, not ADR-0025 C7's persisted message
  semantics. Failed extraction retains only bounded sealed evidence; no live box is kept as an
  artifact. General patch handoff stays outside this mode.
- **S9**: C9 and D9 are owed with the job supervisor. ADR-0026 C4/D4/S10 distinguish phase states
  while preserving failed-handoff exit behavior. A complete run may still have a failed job.
- **S10**: C10 and D10 are owed. The live matrix exercises file tools, shell, symlink races,
  alternate configs, MCP and host routes, then export and teardown. A receipt is invalidated by
  changes to the adapter, host, image, backend, launch policy or suite.
- **S11**: C11 and D11 are owed. All stop paths, including a second interrupt, retain cleanup
  obligations. Failed deletion may leave resources present; it never becomes a clean success.
- **S12**: C12 and D12 are owed. The task submitter sets retention and capacity before use. The
  initial implementation excludes CLI-owned tools, Gemini, arbitrary hooks, nested delegation,
  remote adapters and federation with another store. ADR-0013's resident restrictions are unchanged.
- **S14**: Owed in D10's matrix: positive reads through `read`, `list_dir`, numbered reads and
  search, then denied absolute and parent paths. Exercise symlink swaps during traversal and search,
  newline and option-like names, hard links, worktree pointers and sibling paths. Repeat through
  `bash` and `python`. The canary's value stays outside the declared input set; only targets are
  supplied.
- **S15**: Owed in D10's matrix: synthetic mailbox reads by inbox and known record id; alternate
  `--config`, `KHIVE_CONFIG`, `KHIVE_DB`, ancestor configs and MCP declarations. Test absent configs
  too. Hostile hooks, plugins, instruction files and credential lookup must create no host side
  effect or prompt injection. No probe accesses real user mail.
- **S16**: Owed in D7's live tests: host gateway and LAN listeners, IPv4 and IPv6, DNS over UDP and
  TCP, metadata routes, peer guests and proxy variables. Positive controls establish listener health
  outside the denied route. Closed ports alone prove nothing. Task attempts to change networking
  must leave denial intact.
- **S17**: Deterministic repository tests cover dispatch refusals, path races, launch arguments,
  quotas, state transitions and crash reconciliation. Real-box tests cover the same routes through
  the deployed adapter. Live model challenges cover initial, resumed and retried backend calls,
  including hidden tools; an unavailable route needs observed rejection, not self-reported safety.
- **S18**: Full live measurement is once per unchanged qualified tuple, repeated after upgrades or
  policy changes. Each actual task run repeats canary preflight through its own deterministic tool
  routes before task execution. Record both receipts; a reused class certificate alone is
  insufficient.
- **S19**: Qualification includes a successful report export, missing and oversized reports,
  multibyte byte limits, links and special files, interrupted publication and lost delivery
  receipts. Then verify guest, backend children, per-run volume, socket and canary fixture teardown.
  No pass is issued while cleanup or a positive control is unresolved.
