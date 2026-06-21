import * as vscode from "vscode";
import type { ProjectGroup, Run } from "../api/types.js";

/** Display + group key for runs that have no detected project. */
export const NO_PROJECT = "(no project)";

/**
 * Stable identifier for a run. Mirrored sessions (e.g. Claude transcripts) are
 * returned with `id` set but no `run_id`, so always resolve through both rather
 * than touching `run.run_id` directly (which would throw on those rows).
 */
export function runId(run: Run): string {
  return run.run_id ?? run.id ?? "";
}

const TERMINAL_STATUSES = new Set([
  "succeeded",
  "completed",
  "failed",
  "error",
  "cancelled",
  "done",
]);

export function isTerminal(run: Run): boolean {
  return TERMINAL_STATUSES.has(run.status?.toLowerCase() ?? "");
}

export function statusIcon(run: Run): vscode.ThemeIcon {
  const s = (run.status ?? "").toLowerCase();
  const h = (run.effective_health ?? "").toLowerCase();

  if (s === "running" || s === "active" || s === "starting") {
    return new vscode.ThemeIcon(
      "loading~spin",
      new vscode.ThemeColor("charts.blue")
    );
  }
  if (s === "succeeded" || s === "completed" || h === "healthy") {
    return new vscode.ThemeIcon(
      "pass-filled",
      new vscode.ThemeColor("charts.green")
    );
  }
  if (s === "failed" || s === "error") {
    return new vscode.ThemeIcon(
      "error",
      new vscode.ThemeColor("charts.red")
    );
  }
  if (s === "cancelled") {
    return new vscode.ThemeIcon(
      "circle-slash",
      new vscode.ThemeColor("descriptionForeground")
    );
  }
  if (s === "queued" || s === "pending") {
    return new vscode.ThemeIcon(
      "clock",
      new vscode.ThemeColor("charts.yellow")
    );
  }
  return new vscode.ThemeIcon("circle-outline");
}

/** Normalize an API timestamp (epoch seconds, epoch ms, or ISO string) to epoch ms. */
export function toMillis(
  v: number | string | null | undefined
): number | undefined {
  if (v === null || v === undefined) {
    return undefined;
  }
  if (typeof v === "number") {
    if (!Number.isFinite(v)) {
      return undefined;
    }
    // The backend sends epoch seconds (~1.7e9); guard against ms (~1.7e12).
    return v < 1e11 ? v * 1000 : v;
  }
  const ms = Date.parse(v);
  return Number.isNaN(ms) ? undefined : ms;
}

export function relativeTime(
  ts: number | string | null | undefined
): string {
  const ms = toMillis(ts);
  if (ms === undefined) {
    return "";
  }
  const diffMs = Date.now() - ms;
  if (diffMs < 0) {
    return "just now";
  }
  const secs = Math.floor(diffMs / 1000);
  if (secs < 60) {
    return `${secs}s ago`;
  }
  const mins = Math.floor(secs / 60);
  if (mins < 60) {
    return `${mins}m ago`;
  }
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) {
    return `${hrs}h ago`;
  }
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

/** Last path segment of a project ref: "ohdearquant/lattice" → "lattice". */
export function shortProject(
  project: string | null | undefined
): string | undefined {
  if (!project) {
    return undefined;
  }
  const seg = project.split("/").pop()?.trim();
  return seg || project;
}

/** A name is useful only if set and not just echoing the invocation kind ("agent"). */
function meaningful(
  v: string | null | undefined,
  kind: string | null | undefined
): v is string {
  const s = v?.trim();
  if (!s) {
    return false;
  }
  return !kind || s.toLowerCase() !== kind.toLowerCase();
}

/** The most identifying label available — never the bare invocation kind. */
function pickLabel(run: Run): string {
  const kind = run.invocation_kind;
  if (meaningful(run.name, kind)) {
    return run.name.trim();
  }
  if (meaningful(run.agent_name, kind)) {
    return run.agent_name.trim();
  }
  if (meaningful(run.playbook_name, kind)) {
    return run.playbook_name.trim();
  }
  const proj = shortProject(run.project);
  if (proj) {
    return proj;
  }
  const id = runId(run);
  return kind ? `${kind} ${id.slice(0, 6)}`.trim() : id.slice(0, 8) || "run";
}

const SUCCESS_STATUSES = new Set(["completed", "succeeded", "done"]);

/** Compact detail line: project · size · model · time, with non-success status surfaced. */
function buildDescription(run: Run, label: string): string {
  const parts: string[] = [];
  const proj = shortProject(run.project);
  if (proj && proj !== label) {
    parts.push(proj);
  }
  if (typeof run.message_count === "number" && run.message_count > 0) {
    parts.push(`${run.message_count} msg`);
  }
  if (run.model) {
    parts.push(run.model);
  }
  const rel = relativeTime(run.started_at ?? run.created_at);
  if (rel) {
    parts.push(rel);
  }
  const s = (run.status ?? "").toLowerCase();
  if (s && !SUCCESS_STATUSES.has(s)) {
    parts.push(s);
  }
  return parts.join(" · ");
}

export class RunItem extends vscode.TreeItem {
  constructor(public readonly run: Run) {
    const label = pickLabel(run);
    super(label, vscode.TreeItemCollapsibleState.None);

    this.description = buildDescription(run, label);
    this.iconPath = statusIcon(run);
    this.contextValue = isTerminal(run) ? "runTerminal" : "runActive";
    this.tooltip = buildTooltip(run);
    // Only attach an open command when a stable id is available; rows without
    // one are display-only and must not trigger API calls with an empty id.
    if (runId(run)) {
      this.command = {
        command: "den.openRun",
        title: "Open Run",
        arguments: [run],
      };
    }
  }
}

/** A collapsible project parent. Runs load lazily when it is expanded. */
export class ProjectGroupItem extends vscode.TreeItem {
  readonly key: string;
  constructor(
    public readonly group: ProjectGroup,
    expanded: boolean
  ) {
    super(
      shortProject(group.project) ?? NO_PROJECT,
      expanded
        ? vscode.TreeItemCollapsibleState.Expanded
        : vscode.TreeItemCollapsibleState.Collapsed
    );
    this.key = group.project ?? NO_PROJECT;
    this.description = `${group.count}`;
    this.contextValue = "runGroup";
    this.iconPath = new vscode.ThemeIcon("repo");
    const rel = relativeTime(group.last_activity);
    const noun = group.count === 1 ? "run" : "runs";
    this.tooltip =
      `${group.project ?? NO_PROJECT} · ${group.count} ${noun}` +
      (rel ? ` · ${rel}` : "");
    // Stable id so manual expand/collapse survives the 4s poll refreshes.
    this.id = `group:${this.key}`;
  }
}

/** Pinned top-of-tree group: every currently-running session, flat and cross-project. */
export class ActiveGroupItem extends vscode.TreeItem {
  constructor(count: number) {
    super("Active", vscode.TreeItemCollapsibleState.Expanded);
    this.description = `${count}`;
    this.contextValue = "activeGroup";
    this.iconPath = new vscode.ThemeIcon("zap", new vscode.ThemeColor("charts.blue"));
    const noun = count === 1 ? "session" : "sessions";
    this.tooltip = `${count} running ${noun} across all projects`;
    // Stable id so the group's expanded state survives the 4s poll refreshes.
    this.id = "group:__active__";
  }
}

/** Leaf that pages the next slice of a project's runs when clicked. */
export class LoadMoreItem extends vscode.TreeItem {
  constructor(
    public readonly key: string,
    loaded: number,
    total: number
  ) {
    super(`Load more · ${loaded} of ${total}`, vscode.TreeItemCollapsibleState.None);
    this.contextValue = "loadMore";
    this.iconPath = new vscode.ThemeIcon("ellipsis");
    // id encodes the loaded count so the node re-renders as the group grows.
    this.id = `loadmore:${key}:${loaded}`;
    this.command = {
      command: "den.loadMoreRuns",
      title: "Load more runs",
      arguments: [key],
    };
  }
}

/** Escape characters that carry Markdown link/command semantics. */
function escapeMd(s: string): string {
  // Escape backslash first, then the characters that form Markdown constructs
  // ([, ], (, ), *, _, ~, `, #) so that user-controlled run data cannot form
  // clickable links or other active Markdown when rendered in the tooltip.
  return s.replace(/\\/g, "\\\\").replace(/[[\]()!*_~`#|>]/g, (c) => `\\${c}`);
}

function buildTooltip(run: Run): vscode.MarkdownString {
  const md = new vscode.MarkdownString("", true);
  // isTrusted remains false (the default) so that any command: URI appearing
  // in run-supplied data is rendered as inert text rather than a clickable link.
  md.supportHtml = false;

  const title =
    run.name ?? run.playbook_name ?? run.agent_name ?? runId(run).slice(0, 8);
  md.appendMarkdown(`### ${escapeMd(title)}\n\n`);

  if (run.invocation_kind) {
    md.appendMarkdown(`**Kind:** ${escapeMd(run.invocation_kind)}\n\n`);
  }
  if (run.model || run.provider) {
    const parts = [run.model, run.provider].filter(Boolean).map((v) => escapeMd(v!)).join(" / ");
    md.appendMarkdown(`**Model:** ${parts}\n\n`);
  }
  if (run.effort) {
    md.appendMarkdown(`**Effort:** ${escapeMd(run.effort)}\n\n`);
  }
  if (run.project) {
    md.appendMarkdown(`**Project:** ${escapeMd(run.project)}`);
    if (run.project_source) {
      md.appendMarkdown(` _(${escapeMd(run.project_source)})_`);
    }
    md.appendMarkdown("\n\n");
  }
  md.appendMarkdown(
    `**Branches / Messages:** ${run.branch_count} / ${run.message_count}\n\n`
  );
  if (run.started_at) {
    md.appendMarkdown(`**Started:** ${formatTs(run.started_at)}\n\n`);
  }
  if (run.ended_at) {
    md.appendMarkdown(`**Ended:** ${formatTs(run.ended_at)}\n\n`);
  }
  md.appendMarkdown(`**Status:** ${escapeMd(run.status)}`);
  if (run.effective_health) {
    md.appendMarkdown(` · health: ${escapeMd(run.effective_health)}`);
  }

  return md;
}

function formatTs(ts: number | string): string {
  const ms = toMillis(ts);
  if (ms === undefined) {
    return String(ts);
  }
  return new Date(ms).toLocaleString();
}
