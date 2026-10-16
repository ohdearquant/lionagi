# The Studio model

Studio connects a browser interface to LionAGI work on your machine. The
browser helps you give instructions, inspect execution, and make decisions.
The daemon provides the local services and durable records behind those
views.

## Daemon and client

`li studio` starts a daemon on `127.0.0.1:8765` by default. The hosted client
at `https://lion-studio.khive.ai` connects to that local service. Docker bundles
the client and daemon and publishes a host port; its default port mapping is
not restricted to loopback. A source checkout can run a local development
frontend. The chosen launch mode determines the network exposure.

With `LIONAGI_STUDIO_AUTH_TOKEN` unset, API authentication is disabled: any
caller that can reach the daemon and passes its Host-header check can call
the `/api` endpoints, subject to their normal request validation. The default
`CORS_ORIGINS` includes `https://lion-studio.khive.ai`, so a page served from
that origin can drive the no-token local API, including Operator proposal
approval decisions. Host validation and CORS do not authenticate the caller
or establish that a human approved a request.

Setting `LIONAGI_STUDIO_AUTH_TOKEN` requires the matching bearer token for
API requests and the schema/docs endpoints; `/health` and CORS preflight
remain unauthenticated. The
ordinary hosted client has no token-entry UI and does not receive a token
from the daemon's environment. It therefore does not gain authenticated
access merely because the daemon has a token configured. These behaviors
follow `lionagi/studio/app.py`, `lionagi/studio/config.py`, and
`lionagi/studio/services/operator.py`;
frontend credential handling is in `apps/studio/frontend/src/lib/api.ts`.

This split lets the interface use local agents, files, and stored execution
history. The hosted page does not become the owner of that state. Keep the
daemon running for Operator and automatic scheduling. Closing a browser tab
is different from stopping a turn or shutting down the daemon.

Follow [Get started with Studio](../guides/studio-getting-started.md) to
choose a launch mode.

## Runs, sessions, and branches

| Object | Meaning |
| --- | --- |
| Run | The execution view you browse in Fleet: status, timing, recorded activity, and results of a unit of work. |
| Session | The core execution container that owns one or more branches and coordinates their work. |
| Branch | One stateful conversation and its tools, model configuration, and message history. |
| Invocation | A recorded launch or dispatch that can link the work it started to one or more sessions. |
| Schedule fire | One recorded attempt to execute a schedule, with its own status and linkage to the dispatched work. |

In the current runs service, a Fleet run is a view over persisted session
state. Open its detail to inspect the branches and recorded messages behind
it. A session may have several branches; an agent follow-up therefore requires
a branch choice when more than one is available. An invocation or schedule
fire provides launch context rather than being another name for a branch.

In Python, [`Session`](../api/session.md) owns branches and supports their
message exchange and graph execution. [`Branch`](../api/branch.md) owns the
conversation state used by operations such as `communicate()`, `operate()`,
and `run()`. An in-memory Python conversation alone is not a durable Studio
history. Task-producing CLI paths persist state that Studio can read after
the original interaction, while saved artifacts are a separate output.

Operator also stores its conversation and turn events in the daemon. Reloading
the client reconstructs that history; it does not replay the instruction as a
new request. Later turns can reuse the same conversation branch and run record.
For agent runs, **Continue this run** takes a follow-up instruction and resumes
from a persisted branch snapshot, with a branch choice when needed. Play, flow,
and show-play runs instead offer **Continue** to replay a saved checkpoint,
without an instruction or branch choice. The required branch snapshot or
checkpoint must still exist after retention or cleanup. Unsupported kinds or
missing state produce an explained refusal. Durable records do not mean that
an active process continues through every daemon shutdown. These continuation
paths follow `apps/studio/frontend/src/components/history/ResumeRun.tsx` and
`lionagi/studio/services/run_resume.py`.

See [durable operations](../guides/durable-operations.md) for monitoring,
control, and recovery, and the [Operator guide](../guides/studio-operator.md)
for follow-ups from the UI.

## Definitions describe work; runs record it

Reusable definitions configure future work. Changing a definition and
launching an execution are separate actions.

| Kind | Purpose | Current Studio access |
| --- | --- | --- |
| Agent | A named worker profile with instructions, model and tool-related configuration. | Library agent browsing and profile controls. |
| Playbook | A reusable YAML specification for a task or flow, including parameters and prompts. | Library shows bundled templates and installed playbooks; template detail exposes metadata, inputs, and prompt. |
| Workflow | A stored graph design with nodes and edges. | Backend definition services exist; the Library kind and Designer surface are unavailable. |
| Engine | A domain-specific execution pipeline and its configuration. | Engine definition services and CLI exist; the Library kind is unavailable. System links to engine-run history. |
| Skill | A named set of instructions and supporting resources for a capability. | Library inspects installed skills and their usage. |
| Plugin | A package of related components, such as skills and agents. | Library inspects installed plugins and their contents. |

A bundled playbook template is read-only; cloning materializes a copy to
customize. The current playbook detail shows an agent-and-prompt template's
fields rather than drawing a graph for it. Workflow graph definitions are a
different Library kind. See [orchestration](../guides/orchestration.md) for
execution shapes and [engines](../reference/engines.md) for engine usage.

For the current UI boundary, consult the
[Library availability note](../reference/studio-surfaces.md#currently-unavailable-library-kinds).
Availability follows the frontend's `UNFINISHED_KINDS` set, not the mere
existence of a backend route.

## Projects and teams add context

A project attributes sessions to a named context so Fleet can filter related
work. The Projects API maintains registry metadata and can assign sessions;
there is no separate project-management page. A team is a crew with members
and shared messages, browsed read-only through the Teams page. Neither a
project registry entry nor a team message is itself an execution run.

See [Studio surfaces](../reference/studio-surfaces.md) for the current
registry and team-browser capabilities.

## Approvals cross page boundaries

A launch, run, or Operator turn can require a decision before proceeding.
Studio shows that decision where the work is being inspected: a run's gate,
an attention link, or an Operator permission card. There is no independent
Approvals destination in the rail. Acknowledging an attention item means it
was seen; it does not grant permission. Allowing an Operator operation
applies to that request alone.

Schedules also remain definitions between fires. Each fire is admitted and
executed by the daemon with its configured limits. The
[schedules guide](../guides/studio-schedules.md) covers everyday use;
[ADR-0070](../adr/ADR-0070-studio-scheduling-and-dispatch-delivery.md) records
the scheduling and dispatch delivery decisions.

## Architecture and implementation references

The [Studio information architecture decision](../adr/ADR-0080-studio-six-space-cockpit-information-architecture.md)
records the intended spaces and the distinction between intended and shipped
navigation. Use [Studio surfaces](../reference/studio-surfaces.md) for the
current interface rather than treating every space in that decision as a
page available today.

Contributors working on security, persistence, or protocol behavior should
read [Studio internals](../internals/studio.md). That reference owns the
implementation invariants; this model explains the objects a user interacts
with without duplicating those contracts.
