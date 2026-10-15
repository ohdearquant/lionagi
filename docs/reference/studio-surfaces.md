# Studio surfaces

Use this reference to find the page or API area for a task. For installation
and a first run, start with [Get started with Studio](../guides/studio-getting-started.md).
For the relationship between execution and stored definitions, see
[the Studio model](../concepts/studio-model.md).

The navigation and available actions below are checked against
`apps/studio/frontend/src/routes/` and `components/shell/IconRail.tsx`.
Backend area names refer to the registrations in
`lionagi/studio/services/`; the running daemon's generated schema is the
reference for exact requests and responses.

## Home and Attention

**Home** opens an overview of work running now, recent runs, activity and
reported spend, and items needing attention. Select a run to inspect its
execution. Activity and spend share a time-window selector. A stale-data
indicator distinguishes an interrupted refresh from a current view.

The **Attention** tab expands the overview's compact queue. Filter active,
discharged, or all items. You can acknowledge an item as seen, or resolve,
snooze, or mark an eligible item as expected. Acknowledgement keeps the item
visible. Pending gates only allow acknowledgement here: follow the item to
make the actual approval decision.

Backend areas: runs, invocations, schedules, stats, and attention provide the
execution summaries, activity, spend, and persistent attention dispositions.

## Fleet

Open **Fleet** beside Overview to browse active and past runs. Search the
list, filter it by status or project, and select a row to open its detail.
The project selector includes all projects and runs with no project, plus
searchable project names and counts.

Run detail brings together status, recorded messages and tool results,
branches, graph activity, and file links when present. **Continue this run**
uses a persisted branch for a follow-up; see the
[Operator guide](../guides/studio-operator.md#follow-work-into-fleet-and-runs)
for live-run queuing, branch selection, and the CLI escape hatch.

Backend areas: runs and sessions provide execution history and branch detail;
runs also handles follow-ups, and invocations records launch context.

## Operator

Operator is a dock available across Studio. Send instructions, follow the
stream, decide permission requests, stop a turn, or open its run. The daemon
stores conversations so they can be restored after a browser reload.

Backend areas: operator owns conversations, turns, streamed events, and native
permission decisions; linked execution appears through runs and invocations.
See [Use Operator](../guides/studio-operator.md).

## Schedules

**Schedules** lists standing automations in cards, a table, or a calendar.
Create a schedule, enable or disable it, open its definition, edit it, request
a fire, and inspect recent fire history. A fire with a linked invocation can
open the corresponding execution view.

Backend area: schedules owns definitions, fire requests, history, and limits.
See [Automate work with schedules](../guides/studio-schedules.md).

## Library

**Library** groups reusable definitions. Browse all visible kinds together or
choose a kind tab, search by name, and select an item for its detail.

- **Agents** are named profiles. Inspect their configuration and use the
  creation and editing controls for profiles that are not protected.
- **Playbooks** includes bundled templates and your installed playbooks.
  Inspect their agent, inputs, and prompt; clone a bundled template to
  customize it or launch a playbook. Bundled templates remain read-only.
- **Skills** and **Plugins** expose installed instructions and packaged
  components. Inspect their contents and usage. Install these through their
  filesystem or plugin tooling; Library has no new-skill or new-plugin form.
- **MCP servers** exposes server registrations and their configuration.
- **Hooks** exposes hook assemblies and configuration.

### Currently unavailable Library kinds

Workflow and Engine tabs are hidden, and their items are excluded from the
combined list. Their backend definition services exist, but there is no
usable Designer page in the current navigation. Playbooks is available.

This boundary comes from `UNFINISHED_KINDS` in
`apps/studio/frontend/src/routes/library.tsx`, currently containing
`workflow` and `engine`. Update this callout when entries are removed, and
remove it when the set is empty. The
[Studio information architecture decision](../adr/ADR-0080-studio-six-space-cockpit-information-architecture.md)
records the intended navigation and its implementation boundaries.

Backend areas: agents, playbooks, skills, plugins, mcp, hooks, and operator
supply the visible definitions and hook settings. The workflow-defs and
engine-defs areas exist independently of their hidden tabs.

## Teams

**Teams** browses crews created with `li team`. The list shows a team's name,
member count, and last modification. Open a team to inspect its members and
shared message history, shown newest first. Refresh the list or load more
teams as needed. This page is a read-only browser; team creation and message
sending remain in the [team coordination tools](../api/team.md).

Backend area: teams reads the team JSON files under `~/.lionagi/teams`.
The page is separate from Operator conversation history and Fleet runs.

## System

**System**, near the bottom of the rail, places these sections on one page:

| Section | Current actions and information |
| --- | --- |
| Health | Inspect database and WAL sizes, diagnostics, and phantom-session counts. |
| Maintenance | Run a WAL checkpoint, prune retained execution data, or vacuum the database; inspect the result. Phantom-session pruning asks for confirmation. |
| Admin events | Inspect recorded maintenance and control events; filter by action or target ID and apply the filter. |
| Data | Open engine-run history or the definition browser. |
| Settings | Choose light or dark theme and language; inspect the API base and frontend build version. |

Theme is a browser preference. Language is stored in a browser cookie and
changing it reloads the page. The displayed API base and build version are
read-only. This settings section does not configure provider credentials or
edit the daemon's environment.

Backend area: admin supplies health, maintenance, and audit events. Data
links open the engine-runs and definitions areas; appearance and language are
client preferences.

## Approvals

Approvals are a mechanism used by work that needs a decision, rather than a
separate page. Open an attention item or the relevant run to inspect a gate.
Operator displays its native permission requests directly in the
conversation. Granting one request does not approve all later work.

Backend areas: approvals stores approval requests and decisions; operator
handles its conversation-specific permission flow. Acknowledging an item in
Attention does not grant its pending approval.

## Projects

Projects attribute work to a named context. Fleet's project filter helps you
find matching runs, including those with no attribution. There is no
dedicated Projects page or project-management form in the current UI.

The API also provides a project registry with source, local path, repository,
description, timestamps, and session counts. Its detail includes agents and
playbooks used by the project's sessions. Integrations can create a
Studio-managed project, update its path/repository/description, and assign
specific sessions or all unassigned sessions. Assignment records a manual
attribution. Deleting a registry entry is restricted to Studio-managed
projects and does not delete their sessions.

Backend area: projects owns the registry and assignment operations. Fleet's
project choices come from the runs area, so the filter represents attributed
runs rather than a project-management screen. These behaviors are checked
against `lionagi/studio/services/projects.py` and
`components/fleet/ProjectFilter.tsx`.

## Generated API reference

With the daemon running, open
[its OpenAPI schema](http://127.0.0.1:8765/openapi.json). Use your daemon's
host and port if they differ from the default. Token-protected deployments
require the same bearer authentication for the schema as for API calls.

The schema is generated from the routes mounted by `lionagi/studio/app.py`
and registered through `lionagi/studio/registry.py`. It describes the API of
that running version, including request schemas and response shapes. Consult
it for exact endpoints instead of inferring an API path from a page name.

For scheduling behavior, see
[ADR-0070](../adr/ADR-0070-studio-scheduling-and-dispatch-delivery.md).
