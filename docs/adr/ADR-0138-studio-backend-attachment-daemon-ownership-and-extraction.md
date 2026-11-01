# ADR-0138: Studio backend attachment, daemon ownership, and extraction from the wheels

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: studio
- **Date**: 2026-10-25
- **Relations**: decides the Studio backend form that ADR-0136 D7 deferred and that ADR-0136 Amendment 1
  A4 item 2 requires before dependent changes; adopted together with ADR-0136 Amendment 2, which moves the
  application's extraction to 2.0

This record decides how the Studio application's backend relates to the daemon: which component owns each process resource, route, and credential, which code is shared, and when the application leaves the library wheels.

## Context

Source references (`path:line`) in this record refer to development-line commit `b4a3bebf`. `ADR-0136` denotes `docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md` throughout. Proposed paths, interfaces, configuration names, and verification tests describe the decision, not existing implementation.

Adoption of this record requires the accompanying Amendment 2 to ADR-0136. During 1.x, the application remains at its current `lionagi.studio` paths inside the single distribution. Wave 2 establishes the daemon, route, identity, and shared-service boundaries. At 2.0, the application moves to `apps/studio/backend/src/`, outside both library wheels. The core never gains a dependency on application code. The ADR-0136 D2 exempt set is `{cli, studio}` until Amendment 2 step 6 moves the CLI, `{studio}` from that step through the last 1.x release, and empty from 2.0, when the application leaves the wheel (`ADR-0136 D1`, `ADR-0136 D2`; `tests/test_moved_modules.py:669`).

Amendment 1 already requires the backend record to allocate every scheduler and CLI dependency on Studio, puts shared command logic and parsing in `lionhub.orchestration`, and requires fresh-install local schedule validation and export without the backend. Those are premises of this decision (`ADR-0136 A4 items 2 to 5`). It also leaves automatic plugin discovery deferred; this record selects one explicitly activated application, without changing that decision (`ADR-0136 Amendment 1, What this amendment does not decide`).

The following current behaviors constrain the boundary:

- A directory boundary is insufficient. The scheduler imports `services.scheduler_state`, project resolution, lifecycle reapers, and database maintenance. Reconciliation imports helpers from both `admin` and `shows`. These dependencies must be allocated with the scheduler (`lionagi/studio/scheduler/engine.py:43-50`, :384, :1135, :1542-1556; `lionagi/studio/services/lifecycle.py:15-23`).
- The scheduler-state dependency is not a Python module import cycle. Its chain is engine to scheduler state to coordination; coordination imports only standard-library modules. It needs relocation, not a new inversion framework (`lionagi/studio/scheduler/engine.py:43-50`; `lionagi/studio/services/scheduler_state.py:18-26`; `lionagi/studio/scheduler/coordination.py:16-19`).
- Pairing is already process-local. Codes, capabilities, and the epoch live in module state; the launcher explicitly uses one worker. A second process would require a new verification protocol, not merely another server command (`lionagi/studio/launch_access.py:75-112`, :141-166, :324-353; `lionagi/studio/cli.py:497-503`).
- A decision capability currently supplies attribution, not API admission. Invalid, absent, expired, or wrongly bound capabilities produce no principal; the decision still reaches the coordinator. Turning this into a mandatory credential would change behavior (`lionagi/studio/services/operator.py:311-327`, :335-359, :370-394).
- Neither schedule CLI HTTP client currently supplies `Authorization`. Both construct only a JSON content-type header. A token-protected daemon consequently refuses these clients; splitting packages does not fix that defect (`lionagi/studio/cli.py:1224-1245`; `lionagi/cli/machine_schedule.py:43-69`; `lionagi/studio/app.py:613-615`).
- `_auto.py` is not a usable HTTP host. Its list omits five modules, and its compiler consumes a marker that the active Studio decorator does not attach. Correcting the list alone would not convert the active routes (`lionagi/_auto.py:111-141`, :314-328, :355-371; `lionagi/studio/registry.py:95-122`).

## Decision

The Basis column says where each decision comes from: a direction already set for the project, this record's own judgment, or a question this record leaves open.

| Fork | Decision | Basis |
| --- | --- | --- |
| F1 | Proposed: one explicitly selected Studio application attaches to the daemon in the same process through a fixed `attach_studio` interface; no discovery. A separate local application process is the open alternative. | Open: awaiting a project decision |
| F2 | The daemon owns the scheduler and every other process resource in today's lifespan; the application owns Operator and its recovery. | Project direction |
| F3 | Ten route modules belong to the daemon, nineteen to the application; local schedule validation and export stay local. | This record's judgment |
| F4 | The daemon owns the one identity and capability store; the application calls it in process. | This record's judgment, following F1 |
| F5 | Shared execution code moves into `lionhub` services; application routes call them, never the reverse. | Project direction for the destinations; this record's judgment for the allocation |
| F6 | The unused HTTP module list and loader in `lionagi._auto` are deleted; each owner keeps one route inventory. | This record's judgment |
| F7 | The application stays in the `lionagi` wheel through 1.x and leaves both library wheels at 2.0 for `apps/studio/backend/src/`; its distribution after that is outside this record. | Project direction for the location; this record's judgment for the timing |

### 1. F1: Attach one explicitly enabled backend in the daemon process (proposed)

F1 is proposed and awaits a project decision between this in-process attachment and a separate local application process beside the daemon; F4, and the parts of F2 that place application startup inside the daemon's lifespan, follow whichever form is chosen. The rest of this section describes the proposed form.

Choose an explicitly configured, in-process backend with one fixed attachment interface. The daemon owns the listener, root HTTP application, admission middleware, scheduler, and process lifecycle. The Studio backend owns its application routers, Operator, static frontend mounting, and application startup and shutdown hooks. Its implementation remains under `lionagi/studio` through 1.x and moves to `apps/studio/backend/src/` at 2.0, with Operator at `apps/studio/backend/src/operator/`. It never moves into `lionhub`.

In wave 2, `lionagi.studio.app` provides a fixed `attach_studio(application, services)` function and retains `create_app`, `app`, and the existing launch surfaces. The daemon factory accepts that attachment callable or no callable. Attachment adds the application routes and returns explicit startup and shutdown hooks. The services argument contains the existing daemon-owned launch and access services; it creates no second scheduler, launch supervisor, or capability store. The standalone daemon imports no application module. The Studio launch path imports the retained application only when selected and passes its attachment to the daemon factory. Application handlers call public wheel services rather than CLI command wrappers. This explicit composition preserves the existing app export and launch contracts (`lionagi/studio/app.py:550-557`, `lionagi/studio/app.py:689`; `lionagi/studio/__main__.py:1-7`).

At 2.0 the same attachment moves with the application to `apps/studio/backend/src/`. The Studio launcher imports that fixed attachment from the installed application and passes it to the same daemon factory. No arbitrary module name is accepted. How the extracted application is installed, and how the launcher locates it, is decided with its distribution, outside this record; that locating step is the only change to activation required by extraction, and the callable and lifecycle remain the wave-2 contract.

The interface is deliberately fixed. There is no entry-point enumeration, plugin registry, provider ranking, discovery scan, hot replacement, or backend management API. Compose the routers into the root application so that existing paths, route ordering, OpenAPI, exception translation, and CORS method discovery can remain intact. A nested ASGI mount that silently changes these surfaces is not an equivalent implementation. The current application registers routes and derives CORS methods from that resulting application (`lionagi/studio/app.py:151-160`, :451-478, :627-662).

The daemon factory with no attachment starts the daemon API alone and imports no application module. Its application endpoints return their normal missing-route response. In 1.x, `li studio` explicitly selects the bundled application without requiring a new configuration value or installation step. At 2.0 it requires the extracted application to be installed. An explicitly selected but missing, incompatible, or broken backend fails startup with an actionable installation error. `--no-frontend` continues to mean that the frontend is supplied elsewhere, not that the application backend is absent. Daemon-only startup uses the daemon factory without an attachment in both packaging eras. The present desktop launcher depends on this distinction: it launches `li studio --no-frontend` and supplies its own frontend (`apps/studio/desktop/src-tauri/src/backend.rs:155-173`).

Request failure and cancellation are isolated by ownership of tasks, not by operating-system isolation. A failed request or closed event stream must not cancel the scheduler or unrelated accepted work. The backend owns Operator tasks; the daemon owns scheduler and detached launch tasks. No task group may make every request and both subsystems siblings whose failure cancels the whole group. Process termination, memory exhaustion, blocking code, and native crashes remain shared failures. No claim of sandboxing or independent backend restart is made. Enabling, disabling, upgrading, or recovering a failed configured backend requires restarting the process and therefore its epoch.

This preserves the present single-worker pairing arrangement and one supervised desktop child. The current container image also exposes one listener and needs no inter-service proxy or second supervisor (`lionagi/studio/cli.py:497-503`; `apps/studio/desktop/src-tauri/src/backend.rs:164-188`; `apps/studio/Dockerfile:75-80`).

A general daemon plugin host is rejected because one known attachment needs no discovery or selection machinery, and Amendment 1 leaves automatic bootstrap deferred (`ADR-0136 Amendment 1, What this amendment does not decide`). A separate local application process beside the daemon is the open alternative. It provides stronger failure isolation and lets the application restart without restarting the daemon. It requires pairing state to move out of process memory into a store both processes verify against, authenticated cross-process capability verification, two lifecycles, coordination of state writers, and a second supervised desktop child. The present process-local capability checks and single-child launcher are why this record proposes the in-process form; they are not an assertion that process isolation has no value. If the separate process is chosen, F1 and F4 are replaced by an amendment that specifies that verification, and the daemon-side decisions in F2, F3, F5, and F6 stand.

### 2. F2: The daemon owns process resources and the backend owns Operator recovery

Allocate every operation in the existing lifespan as follows. Shared modules supply functions and values; they never independently start a lifecycle.

| Existing operation and evidence | Owner after separation | Start and stop responsibility |
| --- | --- | --- |
| Git position snapshot, `lionagi/studio/app.py:349-364` | Daemon, using shared code-identity helpers | Capture before mutable process startup; retain the snapshot for identity reporting. |
| Traceback dump, `lionagi/studio/app.py:366-368`, :396 | Daemon | Arm once and disarm once. Preserve its existing opt-in configuration. |
| Startup admission warnings, `lionagi/studio/app.py:370` | Daemon | Emit for the actual listener and admission policy, including daemon-only operation. |
| Settings-driven terminal notification callback, `lionagi/studio/app.py:371-374` | Daemon, calling the existing lifecycle registration seam | Register once at daemon bootstrap. The backend does not install a competing callback. |
| Daemon log handler, `lionagi/studio/app.py:375-377` | Daemon | Enclose startup, serving, and shutdown in the same logging context. |
| Scheduler start and stop, `lionagi/studio/app.py:378`, :404 | Daemon | Own one scheduler instance and all tasks it supervises. |
| Run-row startup reconciliation, `lionagi/studio/app.py:379-380` | Daemon | Reconcile executions whether or not the backend is enabled. |
| Operator startup and shutdown, `lionagi/studio/app.py:381-383`, :402 | Backend | Recover application turns when enabled and stop only its own coordinator tasks. |
| Launch-access epoch, `lionagi/studio/app.py:384-387` | Daemon | Begin one serving epoch, retaining only the current launch's pre-minted codes. |
| Reconnect socket and proof key, `lionagi/studio/app.py:388-389`, :417-419 | Daemon | Start and stop once; the key leaves only over the same-user reconnect socket, and the host's health route answers a reconnect challenge with the port proof under that key (an HMAC of challenge and port, `lionagi/studio/app.py:651-668`), never the key. |
| Transcript mirror, `lionagi/studio/app.py:225-300`, :390, :421 | Daemon | Call a wheel-owned mirror service, keep its stop event and task, and use the current shutdown backstop. |
| Maintenance warmup, `lionagi/studio/app.py:321-344`, :391-392, :420 | Daemon | Schedule off the readiness path; cancel and await it during shutdown. |
| Detached launch shutdown, `lionagi/studio/app.py:398`, :423 | Daemon | Cancel and await launch tasks so their existing terminal-row behavior remains intact (`lionagi/studio/services/launches.py:252-259`). |

Construct and validate the selected application before starting process resources. The successful startup order remains: snapshot, traceback and warnings, notification registration, logging context, scheduler start, daemon reconciliation, optional backend recovery, `begin_serving`, reconnect channel, mirror, deferred warmup, and readiness to serve. Without the backend, omit only backend recovery. Do not rely on a framework to automatically execute the lifespan of a nested mounted application; the root lifecycle explicitly enters the returned backend hook at the designated position.

Readiness to accept requests follows completion of startup, not the deferred checkpoint. `/health` remains the lightweight liveness and reconnect-proof surface. `/api/admin/readiness` retains its store and scheduler assessment and reports the result in the response body. Do not silently change it to a 503 endpoint or add an Operator-store requirement. The current Operator startup intentionally skips recovery when the configured store has no local file; its routes then report the existing unsupported-store behavior (`lionagi/studio/app.py:321-329`, :651-668; `lionagi/studio/services/admin.py:1455-1481`; `lionagi/studio/services/operator.py:65-86`). With a selected backend, completion of its supported startup path is a prerequisite to opening the listener, not a second readiness server.

On a normal stop, withdraw the host from serving new requests and preserve the current cleanup order: close the session live-tail registry, disarm traceback capture, clear the reconnect key and stop its channel, finish warmup, stop the mirror, shut down the backend coordinator when present, shut down detached launches, stop the scheduler, and exit the log context. The current code does not implement a literal reversal of startup (`lionagi/studio/app.py:408-424`).

The new attachment seam must unwind each resource that successfully started if a later startup stage fails or is cancelled. Shutdown must attempt remaining cleanup even when an earlier hook raises; the current lifespan does: its cleanup runs in a `finally` around the yield, shielded from cancellation, and records each step's failure and raises them together after the last step (`lionagi/studio/app.py:393-426`). Startup is not covered: a stage that fails before the yield leaves the resources started before it running. Implement and verify that startup correction as a separate preparatory change. A selected backend startup failure ends this launch after cleanup. Backend absence is a configured mode, not an error recovery policy.

Do not describe scheduler startup as waiting for reconciliation. `start()` creates a running task before the next lifespan statement, and the loop performs recovery and then ticks. The reconciliation docstring's stronger sequencing claim is not enforced by this call order. Introducing a scheduler admission barrier or moving reconciliation earlier is a separate behavior change and is not bundled with relocation (`lionagi/studio/scheduler/engine.py:537-557`, :1092-1097; `lionagi/studio/services/lifecycle.py:810-815`).

Starting a scheduler, reapers, or mirror on each side is rejected because it duplicates ownership. Moving the whole lifespan into the application is rejected because it makes scheduling and run repair depend on installing Studio. The daemon-only requirement and the existing scheduler maintenance calls decide this allocation (`lionagi/studio/scheduler/engine.py:1538-1561`).

### 3. F3: Separate execution API routes from application routes

In wave 2, daemon API routes move under `lionhub.serve` and scheduler domain services move under `lionhub.orchestration`. Application routes stay at their existing `lionagi.studio` paths through 1.x and move to `apps/studio/backend/src/` at 2.0. The HTTP ownership split is fully enforced in wave 2, even while all application files remain installed. Execution control and inspection belong to the daemon; Studio application management belongs to the backend.

The following table allocates all 29 registered modules. Each source is under `lionagi/studio/services/`. The cited line identifies a representative route; ownership covers every decorated route in that module. Helpers used across the boundary are allocated separately in F5.

| Module and source line | HTTP owner | Reason |
| --- | --- | --- |
| `lionagi/studio/services/casts.py:16` | Backend | Studio catalog presentation. |
| `lionagi/studio/services/runs.py:418` | Daemon | Inspect execution records and files without Studio. Preserve existing projections and filters. |
| `lionagi/studio/services/run_resume.py:724`, :744 | Daemon | Inspect resume availability and submit a resumed execution. |
| `lionagi/studio/services/engine_runs.py:190` | Daemon | Inspect engine execution records. |
| `lionagi/studio/services/definitions.py:631` | Backend | Application definition history and editing. |
| `lionagi/studio/services/agents.py:385` | Backend | Application profile management. Shared profile loading is F5. |
| `lionagi/studio/services/playbooks.py:336`, :391 | Backend | Application library management and its launch adapter. Execution supervision is F5. |
| `lionagi/studio/services/shows.py:804` | Backend | Show presentation and streaming. Reaper file helpers are F5. |
| `lionagi/studio/services/skills.py:143` | Backend | Application library inspection and validation. |
| `lionagi/studio/services/plugins.py:373` | Backend | Application catalog presentation. |
| `lionagi/studio/services/mcp_servers.py:598` | Backend | Application server-configuration management. |
| `lionagi/studio/services/teams.py:57` | Backend | Application presentation of team records. |
| `lionagi/studio/services/invocations.py:368`, :474 | Daemon | Invocation and artifact inspection. |
| `lionagi/studio/services/launches.py:394`, :412 | Daemon | Submit and cancel daemon-supervised execution. |
| `lionagi/studio/services/projects.py:288` | Backend | Application project editing and assignment. Shared project lookup is F5. |
| `lionagi/studio/services/identity.py:26` | Daemon | Authenticate the process reached by a launcher. |
| `lionagi/studio/services/pairing.py:43`, :63, :125 | Daemon | Redeem codes, revoke capabilities, and preserve the retired issuance endpoint. |
| `lionagi/studio/services/engine_defs.py:232` | Backend | Application definition editing. Launch-time definition lookup is F5. |
| `lionagi/studio/services/workflow_defs.py:219`, :301 | Backend | Application workflow editing and its execution adapter. |
| `lionagi/studio/services/sessions.py:1791`, :1794, :1890 | Daemon | Session records and execution streams. Preserve labeling behavior. |
| `lionagi/studio/services/run_tags.py:133` | Backend | Application tagging actions. Shared read and storage helpers are F5. |
| `lionagi/studio/services/operator.py:94` | Backend | Conversations, proposals, decisions, and application effects. |
| `lionagi/studio/services/hooks_library.py:190` | Backend | Application hook-library editing. |
| `lionagi/studio/services/operator_hooks.py:126` | Backend | Operator configuration. |
| `lionagi/studio/services/approvals.py:546` | Backend | Application approval presentation and decisions. Lower-layer execution guards remain where their callers require them. |
| `lionagi/studio/services/admin.py:1443`, :1456, :1550 | Daemon | Operational diagnosis, readiness, transitions, and maintenance. Preserve current admission policy. |
| `lionagi/studio/services/schedules.py:1584`, :1724 | Daemon | Schedule CRUD, triggering, status, limits, and history. |
| `lionagi/studio/services/stats.py:111` | Backend | Application activity and spending summaries. |
| `lionagi/studio/services/attention.py:377` | Backend | Application attention dispositions and history. |

There are ten daemon modules and nineteen application modules. The original registry is the coverage source (`lionagi/studio/registry.py:44-74`). The host also owns `/health` and the security wrapping of schema and documentation routes. The backend owns frontend assets and SPA fallback. An unknown `/api` path must remain a JSON 404 even when the SPA is installed (`lionagi/studio/app.py:41-54`, :514-547, :651-668).

Backend absence does not imply that every existing CLI command becomes an HTTP client. Keep local schedule validation and export local. The machine validator resolves a document without a daemon, and export reads stored rows and converts them locally; the human export does the same (`lionagi/cli/machine_schedule.py:310-355`, :358-413; `lionagi/studio/cli.py:1951-2023`). No application module may be imported transitively by those paths. This implements A4 item 5 in wave 2 even while the compatibility application is still installed (`ADR-0136 A4 item 5`).

The required new proof is named `tests/packaging/test_studio_backend_boundary.py::test_fresh_wheel_schedule_validate_and_export_without_backend`. During 1.x it installs the normal built `lionagi[studio]` wheel in a fresh environment and runs outside the checkout with the application disabled. Because D1 still requires the application files to be installed, the test also installs a test-only import blocker for the precise retained application modules and asserts that none load. It does not label a normal 1.x wheel physically backend-free. The deny list excludes empty parent packages needed by old compatibility names. At 2.0 the same test installs the released core and hub wheels with required scheduling dependencies and no application installed, so physical backend absence is tested. Both arms use temporary configuration and state. It exercises installed `li schedule validate` and `li schedule export`, plus the installed machine schedule entry points. It covers valid and invalid cron, interval, command, flow, and playbook declarations, populated managed and legacy export fixtures, and partial-export results. It asserts that validation creates no database and that export preserves the input rows. It checks that no backend, FastAPI route module, or running daemon is needed by the local conversion paths. An empty export and a mocked converter are insufficient evidence.

The 1.x test uses `lionagi[studio]` because cron validation imports `croniter`, which is currently in that extra (`lionagi/studio/services/schedule_declaration.py:361-366`; `pyproject.toml:141-146`). The 2.0 test uses the installation declared by the core/hub distribution record with the same required scheduling dependencies. The HTTP dependencies may be installed but the local validators must not import the HTTP adapters. Do not add a new packaging extra merely to rename this existing dependency set.

Also verify the complete enabled route contract, including methods, paths, static-before-parameter ordering, trailing-slash behavior, status codes, schemas, and middleware. Check the daemon-only route set against this table. This installation test is a proposed acceptance requirement, not an existing passing test.

Putting every route in the backend is rejected because schedule control, identity, pairing, execution inspection, and daemon maintenance would then disappear when the application is absent. Putting all routes in the daemon is rejected because Operator and application editors would remain wheel dependencies contrary to the ADR-0136 D7 boundary. The schedule client's concrete HTTP dependency and the desktop's identity handshake establish the minimum daemon surfaces; retaining execution inspection is the explicit product decision (`lionagi/cli/machine_schedule.py:54`; `apps/studio/desktop/src-tauri/src/backend.rs:203-211`).

### 4. F4: Keep one daemon-owned identity and capability store

The daemon owns the configured bearer policy, launch bearer, pairing codes, epoch, decision-capability hashes, revocation, reconnect channel, and port proof. These move together into `lionhub.serve`. Application code calls the same daemon-owned capability resolver in the same process. There is no second secret store, signed-token format, introspection endpoint, or capability replication.

Preserve the existing launch and reconnect flow. A launcher obtains a code in the daemon process or through the same-user reconnect channel, verifies the listener's proof when reconnecting, and opens the browser with the code in the fragment. The browser redeems it at `/api/studio/pair` for the admission token and decision token. Preserve single use, origin binding, the 120-second pairing lifetime, the eight-hour decision lifetime, revocation, and invalidation on a new daemon epoch (`lionagi/studio/cli.py:318-400`; `lionagi/studio/launch_access.py:59-68`, :141-166, :248-276, :301-361; `lionagi/studio/services/pairing.py:63-100`). The retired code-issuance endpoint continues to return 410; do not restore issuance over HTTP (`lionagi/studio/services/pairing.py:35-59`).

Keep the host's admission middleware outside both route sets. Preserve Host checking, configured-token admission, no-token Origin and Fetch Metadata checks, JSON requirements, and the existing pairing exemption. Static page loading remains distinct from API admission (`lionagi/studio/app.py:452-470`, :570-625, :653-662). Browser decisions pass the original request origin and decision header into the one resolver. A resolved principal is not a verified human identity, and a missing or invalid decision token remains unattributed under the existing API policy (`lionagi/studio/launch_access.py:93-101`; `lionagi/studio/services/operator.py:311-327`).

The desktop keeps one child, its generated configured token, its cross-origin frontend setting, and the authenticated `/api/identity` handshake. That endpoint must continue to reject an open daemon even though other no-token requests may be admitted (`apps/studio/desktop/src-tauri/src/backend.rs:136-171`, :203-258; `lionagi/studio/services/identity.py:26-42`). Desktop setup continues to use the bundled application during 1.x; at 2.0 it installs the extracted application as its distribution specifies. It must not treat daemon liveness alone as proof that the required application was installed; the launcher validates the selected application before its listener opens.

The CLI uses the same daemon address resolution, including `LIONAGI_STUDIO_URL`, and retains local validation and export. On an open daemon, current requests without browser headers are admitted. Against a configured-token daemon, both current schedule clients are refused because they omit the token (`lionagi/studio/cli.py:1186-1205`, :1224-1245; `lionagi/cli/machine_schedule.py:54-69`; `lionagi/studio/app.py:123-137`, :591-603). Preserve and document that result during the mechanical move. The proposed correction is a separate additive client change that sends the explicitly configured daemon token in `Authorization` to the configured daemon origin, preserves the no-token mode, and never forwards the token to a different origin. It must cover both clients and their timeout behavior. It is not an implicit consequence of relocating `_base_url`.

Application-owned pairing state is rejected because it would become the authority for a daemon launch and could diverge from its epoch. Self-contained signed capabilities and per-request network verification are rejected because neither is required within one process. If F1 resolves to a separate application process, the amendment that replaces this section must specify online verification by the issuing daemon, revocation and epoch failure behavior, authenticated transport, and which component commits the decision; this record does not specify that design.

### 5. F5: Put shared execution code in the wheel and keep application ownership explicit

The core never names `lionhub`. New shared helpers required only by CLI, daemon, and Studio belong to `lionhub`, not to a new core facade. A core-facing interface is warranted only by an actual lower-layer caller, and must not import a higher layer. Amendment 1 A4 item 3 already assigns shared command logic and parsing to `lionhub.orchestration`; item 6 names `lionhub.serve.cli`, subject to the CLI record. The CLI record still owns the public CLI contract and ownership of `li` at 2.0 (`ADR-0136 A4 items 3 to 6`, `ADR-0136 D7`).

| Concern | Allocation and dependency fence | Evidence at the source revision |
| --- | --- | --- |
| Scheduler state and coordination | Move `scheduler_state` with `SchedulerStateService` and its database implementation into `lionhub.orchestration.scheduler`. Keep `coordination` as its standard-library leaf. The engine imports the service; neither imports an application router. Retain existing injection rather than adding another abstraction. | `lionagi/studio/scheduler/engine.py:43-50`; `lionagi/studio/services/scheduler_state.py:18-31`; `lionagi/studio/scheduler/coordination.py:16-19`. |
| Configuration and timezone resolution | Wheel-owned modules provide listener, scheduler, execution limits, reaper, maintenance, and mirror settings. Preserve environment variable names, defaults, and timezone semantics. Operator-only execution-root configuration and frontend mounting belong to the backend. No shared configuration module imports the backend to fill defaults. | `lionagi/studio/config.py:365-411`, :428-528, :576-641, :709-732; `lionagi/studio/app.py:504-511`. |
| Project resolution | Move shared profile/project detection and the project lookup used for execution-root resolution into wheel services. Project editing routes stay in the backend and use the same stored records. Preserve path precedence and provenance fields. | `lionagi/studio/scheduler/engine.py:356-430`, :765; `lionagi/studio/services/schedule_declaration.py:686-745`, :1132; `lionagi/studio/cli.py:2333`; `lionagi/studio/services/projects.py:288-339`. |
| Maintenance and retention | Daemon-owned wheel services perform checkpoint, prune, archive, and vacuum work. The scheduler and admin adapters call the same implementation. Preserve existing archive and partial-commit semantics; do not establish another maintenance loop in the application. | `lionagi/studio/services/db_maintenance.py:23`, :183-221, :736-936; `lionagi/studio/scheduler/engine.py:1135-1159`, :1551-1561. |
| Lifecycle reapers | Move all execution reconciliation into wheel services, including invocation, session, play, show, and schedule-run repair. Extract process-liveness and show-artifact helpers from route-bearing modules first. Operator conversation recovery remains application-owned. | `lionagi/studio/services/lifecycle.py:15-23`, :805-846; `lionagi/studio/services/operator.py:65-90`. |
| Schedule declarations and export | Move parsing, validation, resolution, planning, applying, and conversion into `lionhub.orchestration.schedule` services. Extract validators from `services.schedules` so local parsing does not import FastAPI. Both CLI forms and HTTP adapters use the same functions. | `lionagi/studio/services/schedule_declaration.py:309-318`, :361-393, :482, :534, :812-836, :1665-1907; `lionagi/studio/services/schedule_export.py:20-44`, :419-551; `lionagi/studio/services/schedules.py:17-28`. |
| Shared command parsing | Wheel-owned parsing and argv helpers hold schedule parser construction, request construction, normalization, address resolution, timeout wording, and flow-option declarations. The CLI imports these helpers instead of Studio. Scheduler subprocess validation derives reserved flow flags from the same parser declaration, without importing command execution handlers. | `lionagi/cli/machine_schedule.py:38`, :71-74, :134, :441, :513; `lionagi/studio/cli.py:1186-1263`, :1402, :2395; `lionagi/studio/scheduler/subprocess.py:265-289`. |
| Mirror and diagnostics | Move the mirror implementation and its configuration dependencies to wheel services consumed by CLI and daemon. Keep the startup code-identity snapshot and process diagnostics there too. | `lionagi/studio/app.py:225-300`, :349-364; `lionagi/cli/mirror.py:21`; `lionagi/studio/config.py:732`. |
| Detached launches and execution definitions | Daemon-owned services retain admission, task handles, cancellation lookup, and terminal writes. Extract launch-time engine-definition lookup from the application CRUD module. Application playbook and workflow launch adapters call the same supervisor. | `lionagi/studio/services/launches.py:182-259`; `lionagi/studio/services/playbooks.py:391`; `lionagi/studio/services/workflow_defs.py:301`. |
| Execution inspection dependencies | Wheel services retain shared session/run projections, tags storage and reads, artifact verification, signals, stream transport helpers, and safe path handling used by daemon routes. Move shared redaction and the pure control-consumer predicate out of Operator modules; the Operator coordinator and proposal adapters stay in the backend. | `lionagi/studio/services/runs.py:16-18`, :213, :350; `lionagi/studio/services/sessions.py:24-30`, :479, :1804-1865; `lionagi/studio/services/engine_runs.py:16`; `lionagi/studio/operator/run_control.py:192-221`. |

Keeping a route in the backend does not mean moving every storage helper in its current file there. Conversely, extracting a helper does not authorize putting an application route back in the daemon. The final dependency direction is application to wheel services to existing core services. Daemon-only execution and local CLI commands must work without importing the application. The explicit Studio launch adapter is the sole CLI path permitted to select it. In 1.x this is enforced with the application installed but disabled and blocked from import; at 2.0 it is enforced with the application absent. The loaded backend can use in-process services without adding HTTP calls between components in the same process.

Retain existing stores and identifiers. The daemon owns execution and scheduler task handles; the backend owns Operator task handles and application state semantics. Physical storage need not be split to establish those owners. Existing persisted paths, status values, notification registration, and database layouts are not rewritten because code ownership changes. Apply the existing deprecation and persisted-name obligations when a compatible move occurs (ADR-0136 D4 items 1 and 2, D5).

Perform helper extraction, dependency correction, and lifecycle failure cleanup as explicit preparatory changes before package moves. The new attachment seam requires that cleanup; it is a separately specified behavior correction, not part of a file rename. This follows the existing requirement that actual moves contain import rewrites rather than hidden behavioral refactoring (ADR-0136 D6). The table above does not authorize relocating the whole Operator package into wheel services to satisfy a transitive import.

A broad `shared` package containing copied application modules is rejected because it hides ownership and retains route imports. Moving shared application-derived policy into core merely to break an import cycle is rejected because the demonstrated callers are higher-layer clients. The existing scheduler service interface and the concrete CLI imports favor moving coherent services and parser declarations instead.

### 6. F6: Delete the unused HTTP list and keep one route inventory per owner

Delete `_auto.py`'s private `_HTTP_MODULES` list and its unused HTTP loading path in A4 step 5. Deletion satisfies the requirement that the list leave the core. The populated CLI catalog and its selection and loading entry points have already left the core in step 3; preserve their behavior at the new home. Generic declaration types can remain only if they name no hub modules. No new HTTP host is built from the compiler (`ADR-0136 A4 items 3 to 5`). The import path `lionagi._auto` itself remains available through 1.x; removing unused private HTTP machinery does not remove that module.

Use the active registry's route metadata as the migration source. Split its registrations between daemon-owned and backend-owned inventories according to F3, and compose them deterministically when the application is selected. The host never imports an application inventory in daemon-only mode. Avoid two independently maintained lists for the same routes. Cross-inventory method-and-path collisions fail application construction, and relative order within each existing route family remains explicit.

The evidence is decisive: the active registry lists 29 modules; the unused list has 24 and omits `identity`, `pairing`, `hooks_library`, `operator_hooks`, and `attention` (`lionagi/studio/registry.py:44-74`; `lionagi/_auto.py:116-141`). Moreover, the latter loader compiles `_lionagi_auto_marker` attributes, whereas `studio_route` appends `StudioRoute` records to its own registry (`lionagi/_auto.py:111`, :321-328, :355-363; `lionagi/studio/registry.py:95-122`). Tests of synthetic compiler markers are not evidence that the real application uses it.

Correcting the five omissions and adopting the compiler is rejected because the decorator mismatch still requires a second route migration with no benefit to this boundary. Leaving the misleading list dormant is rejected because it remains an incorrect starting point for a daemon host. Deleting the unused path is smaller than maintaining a shadow HTTP registry.

### 7. F7: Keep the application in the wheel through 1.x and extract it from the library wheels at 2.0

The location follows the project's direction. The timing is this record's judgment: the application leaves the wheel in the same release that removes every other compatibility path (`ADR-0136 D4`).

The 1.x `lionagi` distribution continues to contain both import roots and the retained Studio application. The `studio` extra supplies the current HTTP and scheduling dependencies. The frontend remains a separate static build. A 1.x Studio installation needs no new backend distribution. The current wheel roots and extra already support this arrangement (`pyproject.toml:141-146`, `pyproject.toml:276-277`; `ADR-0136 D1`). The ADR-0136 D2 exempt set is `{cli, studio}` until Amendment 2 step 6 moves the CLI, `{studio}` from that step through the last 1.x release, and empty from 2.0.

At 2.0, the core `lionagi` wheel contains neither hub nor application code. The separate `lionhub` distribution contains the daemon and scheduler, under ADR-0136 D1's distribution split. The application leaves both library wheels. Its code lives at `apps/studio/backend/src/`, with Operator at `apps/studio/backend/src/operator/`, and never moves into `lionhub`. How the extracted application and the frontend are packaged, distributed, and released is outside this record. Console-script ownership and the core/hub file-ownership transition remain the obligations of the CLI and distribution records (`ADR-0136 D1`, `ADR-0136 D7`).

Whatever distribution is chosen must keep the boundary this record fixes:

| Installation | Required behavior |
| --- | --- |
| A 1.x wheel with required daemon dependencies and no application attachment. | The daemon API and local schedule validation/export work. Application files remain installed for compatibility. |
| A 1.x wheel with its bundled application selected and the matching frontend. | Studio runs through its established launch paths, with the existing optional execution prerequisites. |
| Core and hub 2.0 wheels with required daemon dependencies and no application. | The daemon API and local schedule validation/export work. Studio launch reports the missing application. |
| Core and hub 2.0 wheels with the extracted application installed. | Studio runs with the wave-2 attachment and HTTP contracts and the existing optional execution prerequisites. |
| An explicitly selected application that is incompatible with the installed wheels. | Startup fails before application recovery or scheduler startup. |

The 2.0 transition creates a new environment instead of overlaying a separately installed hub onto a bundled 1.x hub. This avoids overlapping file ownership while allowing the prior complete 1.x environment to remain available for rollback. It must pass the distribution record's fresh-install, upgrade, uninstall, and rollback checks before release (`ADR-0136 D1`). No upgrade or rollback runs two scheduler processes on the same state. Backend relocation does not itself change the state schema or require a new migration framework.

Extracting the application during 1.x is rejected. A separate application distribution or extra would remove `lionagi.studio` imports that an installed 1.x wheel otherwise satisfies, before the release that removes every compatibility path (`pyproject.toml:141-146`; `ADR-0136 D1`, `ADR-0136 D4`).

## Consequences

Under F1's proposed form, the design keeps the existing one-process pairing and desktop supervision shape, and the daemon remains exposed to a selected backend's process-level failures; the open alternative trades that exposure for a cross-process verification design. Either way the daemon API runs without the Studio application, and shared services must be separated from route-bearing files before packages move.

Configuration names and successful request contracts remain stable through mechanical moves; authentication corrections and lifecycle failure cleanup are separate changes with their own verification. A code-only rollback does not prove that persisted state can be rolled back.

ADR-0136 Amendment 2 preserves its D1 and extends the existing Studio exemption until 2.0. Wave 2 removes daemon and shared dependencies on application code but retains the application files for compatibility. The costs are temporary application weight in the wheel and explicit packaging work at 2.0. The final extraction changes file placement, imports, and activation source; it does not postpone the route, identity, or lifecycle design. The application's distribution after extraction is decided separately and must meet the installation behavior in F7.

## Alternatives considered

| Fork | Choice | Rejected alternative and deciding evidence |
| --- | --- | --- |
| F1 | Proposed: Studio attaches explicitly in the daemon process. A separate local application process remains open, awaiting a project decision. | A separate process improves crash isolation but adds cross-process capability verification, a pairing store outside process memory, and another lifecycle to the current single-process launcher (`lionagi/studio/launch_access.py:75-112`; `apps/studio/desktop/src-tauri/src/backend.rs:155-188`). A general plugin host adds discovery that remains deferred (`ADR-0136 Amendment 1, What this amendment does not decide`). |
| F2 | The daemon owns process resources and the application owns Operator recovery. | Application-owned scheduling makes execution depend on Studio; duplicate lifespans duplicate maintenance ownership (`lionagi/studio/app.py:348-426`; `lionagi/studio/scheduler/engine.py:1538-1561`). |
| F3 | Ten route modules belong to the daemon and nineteen to the application. | Moving every route outside removes schedule and identity APIs; keeping all routes in the daemon defeats application extraction (`lionagi/studio/registry.py:44-74`; `lionagi/cli/machine_schedule.py:54`; `lionagi/studio/services/identity.py:26-42`). |
| F4 | The daemon owns the sole epoch and capability store. | Application-owned copies diverge from the issuing epoch. Network verification and signed capabilities are unnecessary in the selected process (`lionagi/studio/launch_access.py:141-166`, `lionagi/studio/launch_access.py:324-353`). |
| F5 | Public wheel services and parsing are shared. | Moving route-bearing modules wholesale retains dependency inversions; new core interfaces for exclusively higher-layer callers add unnecessary machinery. A4 already fixes the shared command direction (`ADR-0136 A4 items 2 to 4`; `lionagi/studio/services/schedule_declaration.py:482`). |
| F6 | The unused HTTP list and loading path are deleted. | Correcting its omissions still leaves a compiler/decorator mismatch; retaining it leaves a stale second inventory (`lionagi/_auto.py:113-141`, `lionagi/_auto.py:321-363`; `lionagi/studio/registry.py:95-122`). |
| F7 | The application stays in the wheel through 1.x and leaves both library wheels at 2.0 for `apps/studio/backend/src/`. | Extraction during 1.x breaks otherwise satisfied old imports before the compatibility removal release (`pyproject.toml:141-146`; `ADR-0136 D1`, `ADR-0136 D4`). Distribution after extraction is outside this record. |
