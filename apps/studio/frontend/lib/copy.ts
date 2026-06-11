// Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
// SPDX-License-Identifier: Apache-2.0
//
// Studio microcopy — single source of truth for UI strings that
// previously drifted across page files.
//
// Conventions (enforce here, not at the call site):
//   • Sentence case. No exclamation, no emoji.
//   • Every sentence ends with a period — including short ones.
//   • Multi-line messages use real `\n`, not `<br>`.
//   • Empty states: "No X yet." (zero-state, page awaits first entry)
//                   "No matching X." (filter result is empty)
//                   "No X found." (lookup miss / search result)
//                   "No X detected." (diagnostic / health check result)
//   • Errors: "Failed to <verb> <object>." — verb-first.
//   • Confirmations: question + scope + permanence, three short lines.
//   • Status pill labels live in StatusPill.tsx (taxonomy-keyed); not duplicated here.
//
// Adding a new string? Pick the category (confirm | empty | error),
// follow the convention, and prefer reuse over a near-duplicate.
//
// i18n is NOT a goal of this module — it's a consistency gate. When
// i18n becomes a roadmap item, this file is the natural extraction
// point.

// ── Not-yet-implemented placeholders ────────────────────────────────
//
// Use `notImplemented.runPlaybook` as the title/tooltip on Run buttons,
// and `notImplemented.newPlaybook` / `notImplemented.newAgent` as the
// hold-message body on the corresponding new-item pages.
// CLI command strings are monospace-marked at the call site, not here.

export const notImplemented = {
  runPlaybook: "Run from Studio not yet implemented — use `li play <name>` from the CLI.",
  newPlaybook:
    "Creating playbooks from Studio is not yet implemented. Use `li play` or author a YAML file directly.",
  newAgent:
    "Creating agents from Studio is not yet implemented. Use `li agent` or author an agent YAML file directly.",
} as const;

// ── Destructive confirmations ────────────────────────────────────────

/**
 * Confirmation copy for pruning phantom sessions.
 *
 * @param count number of sessions to prune
 * @param all true → "Prune all N phantom sessions?", false → "Prune N phantom sessions?"
 */
export function confirmPhantomPrune(count: number, all: boolean): string {
  const noun = count === 1 ? "session" : "sessions";
  const lead = all ? `Prune all ${count} phantom ${noun}?` : `Prune ${count} phantom ${noun}?`;
  return `${lead}\n\nRemoves DB rows. Artifacts on disk are kept.\nCannot be undone.`;
}

// ── Empty states ────────────────────────────────────────────────────

export const empty = {
  // Zero-state — surface awaits its first entry.
  projects: "No projects yet.",
  invocations: "No invocations yet.",
  schedules: "No schedules yet.",
  versions: "No versions yet.",
  plays: "No plays yet.",
  teams: "No teams yet.",

  // Lookup misses — page loaded successfully but found nothing.
  projectsNotFound: "No projects found.",
  plugins: "No plugins found.",
  agents: "No agents found.",
  playbooks: "No playbooks found.",
  skills: "No skills found.",
  shows: "No shows found.",
  runs: "No runs found.",
  engineRuns: "No engine runs found.",
  teamsNotFound: "No teams found.",

  // Filter results — user typed a filter and nothing matched.
  pluginsFiltered: "No matching plugins.",
  agentsFiltered: "No matching agents.",
  skillsFiltered: "No matching skills.",

  // Diagnostic results — health check / detector ran and found nothing.
  phantomSessions: "No phantom sessions detected.",
  branchErrors: "No errors detected across all branches.",

  // Loading bridges — async fetch in flight.
  loadingShows: "Loading shows...",
} as const;

// ── Error toasts ────────────────────────────────────────────────────
// Exported as `errors` (plural) to avoid shadowing local `error` state
// variables that many page components use to hold the currently-
// displayed error message.

export const errors = {
  // Validation — operator input invalid.
  nameRequired: "Name is required.",

  // Fetch failures — page or section couldn't load.
  loadProjects: "Failed to load projects.",
  loadProject: "Failed to load project.",
  loadInvocations: "Failed to load invocations.",
  loadInvocation: "Failed to load invocation.",
  loadDiagnostics: "Failed to load diagnostics.",
  loadSchedules: "Failed to load schedules.",
  loadTeams: "Failed to load teams.",
  loadRuns: "Failed to load runs.",
  loadEngineRuns: "Failed to load engine runs.",
  teamNotFound: "Team not found.",

  // Action failures — operator tried to do something, it failed.
  prune: "Failed to prune sessions.",
  pruneAll: "Failed to prune all phantom sessions.",
} as const;
