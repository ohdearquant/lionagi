# ADR-0014: CLI-Primary, Studio-Secondary

**Status**: Accepted
**Date**: 2026-05-20

## Context

Lion Studio coexists with the lionagi CLI (`li agent`, `li play`, `li o flow`,
`li o fanout`). Both surfaces access the same data: playbooks, agents, sessions,
shows, definitions. The question is which surface is primary for each operation.

During the ADR-0012 design review, several recommendations assumed Studio was
the primary creation and execution surface:

- "Add a Run dialog with input variables and execution mode"
- "Add 'Clone existing agent' as a secondary creation path"
- "Empty states should explain what to do next"
- "After creation, redirect to the new agent's detail and show clear next actions"

These recommendations optimize for a user who lives in the browser. Lion Studio's
actual user lives in the terminal. Playbooks are authored in an editor
and run via `li play`. Agents are defined as markdown files. Shows are orchestrated
by Claude Code agents using the show skill. The CLI is the creation and execution
surface; Studio is the observation and editing surface.

This distinction affects every feature decision.

## Decision

**The CLI is the primary interface for creation and execution. Studio is the
primary interface for observation, inspection, editing, and debugging.**

### What Studio is for

| Capability | Example |
|-----------|---------|
| Observe | Dashboard metrics, session list, show progress |
| Inspect | Run detail with branches/messages, error grouping, file lists |
| Edit | Agent definitions, playbook YAML, with version history |
| Debug | Drill from show → play → session → messages → tool calls |
| Browse | Plugin/skill/agent catalog with source information |

### What Studio is NOT for

| Capability | Why not | Where instead |
|-----------|---------|---------------|
| Run a playbook with parameters | CLI handles input binding, worktree setup, team coordination | `li play playbook.yaml --input ...` |
| Create agents from scratch | Agents are markdown files with frontmatter; a text editor is better | `$EDITOR .lionagi/agents/my-agent.md` |
| Install/remove plugins | Claude Code manages plugin lifecycle | `claude plugin add/remove` |
| Orchestrate shows | Show skill is a Claude Code agent skill, not a UI workflow | `/show topic` in Claude Code |
| Manage CLI configuration | Settings are YAML files | `.lionagi/settings.yaml` |

### The "Run" button exception

The playbook detail page has a "Run" button. This is a convenience shortcut that
shells out to the CLI, not a full execution surface. It does not support:

- Input variable binding
- Execution mode selection
- Team mode configuration
- Worktree customization

If these are needed, use the CLI. The Run button is for "re-run this playbook
with defaults" — a debugging convenience, not a production workflow.

### Implications for UI design

1. **Empty states are diagnostic, not onboarding.** "No sessions" means the data
   hasn't been imported or nothing has run, not that the user doesn't know how to
   create a session. Show scan paths and timestamps, not tutorial copy.

2. **Creation flows are minimal.** New Agent and New Playbook pages exist for
   quick scaffolding, not as the primary authoring surface. They create a file
   and redirect to the editor.

3. **The terminal is always one `!` away.** Users can type `! li play ...` in
   Claude Code to execute from the same session. Studio doesn't need to replicate
   CLI capabilities.

4. **Navigation depth is acceptable.** The 3-click path to a skill (nav → plugin →
   skill) is fine because Studio is for browsing, not for invoking. If you need
   to invoke a skill, you type `/skill-name` in the terminal.

5. **Search is for finding, not for doing.** When global search ships, it routes
   to detail pages for inspection, not to execution dialogs.

## Consequences

**Positive**

- Studio stays focused: observe + inspect + edit + debug. No feature creep toward
  replicating CLI capabilities in the browser.
- Simpler UI: no complex forms for run configuration, input binding, or team setup.
- The CLI can evolve independently. New execution modes don't require Studio changes.
- Correct mental model: Studio is a dashboard/IDE, not a control plane.

**Negative**

- Collaborators who don't use the CLI cannot create or run things from Studio alone.
  This is acceptable for the current user base (a single primary operator + occasional collaborators).
- The Run button on playbooks is a partial exception that may confuse expectations.
  Mitigate by keeping it visually de-emphasized and not adding execution options.
- Some inspector features (e.g., "re-run this failed play") require switching to
  the terminal. A "copy CLI command" button could bridge this gap without making
  Studio an execution surface.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Studio as full execution surface | Replicates CLI complexity in the browser. Two execution surfaces means two places for bugs, two configuration systems, two sets of edge cases. |
| Studio as read-only dashboard (no editing) | Too restrictive. Editing definitions in the browser with version history is genuinely useful and doesn't conflict with CLI-primary. |
| Remove the Run button entirely | The convenience of "quick re-run with defaults" is worth the minimal UI it requires. Just don't expand it. |
