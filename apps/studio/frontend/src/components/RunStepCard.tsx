import React, { lazy, Suspense, useCallback, useMemo, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import Badge from "@/components/ui/Badge";
import FilterChip from "@/components/ui/FilterChip";
import StatCard from "@/components/ui/StatCard";
import {
  IconArrowUpRight,
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconDotFilled,
  IconFile,
  IconGlobe,
  IconPencil,
  IconPlus,
  IconSearch,
  IconTerminal,
} from "@/components/ui/icons";
import type { RunMessage, RunStep } from "@/lib/types";
import type { FileResolutionContext } from "@/components/ui/Markdown";

const Markdown = lazy(() => import("@/components/ui/Markdown"));

interface RolesBreakdown {
  system?: number;
  user?: number;
  assistant?: number;
  tool_call?: number;
  action?: number;
  [role: string]: number | undefined;
}

interface StepResult {
  agent?: string;
  model?: string;
  message_count?: number;
  duration_sec?: number;
  roles?: RolesBreakdown;
  [key: string]: unknown;
}

export interface RunStepCardProps {
  step: RunStep;
  defaultExpanded?: boolean;
  expanded?: boolean;
  onToggleExpand?: (stepId: string, next: boolean) => void;
  /** Run id — enables file-link resolution/viewing in rendered messages. */
  runId?: string;
  /** Run's artifact save root (absolute path), for the agent-dir-first
   * resolution fallback (requirement: bare/relative names resolve against
   * the emitting agent's own artifact subdir first, then the run root). */
  artifactRoot?: string | null;
  /** Known file surface for the WHOLE run (union across all steps/agents),
   * so a bare filename this step didn't itself touch can still resolve
   * against a sibling agent's output — the run's save-root fallback. */
  runFiles?: string[];
}

const STATUS_TONE: Record<string, "ok" | "pending" | "failed"> = {
  completed: "ok",
  running: "pending",
  failed: "failed",
};

const TOOL_ICONS: Record<string, React.ReactNode> = {
  exec_command: <IconTerminal size={12} strokeWidth={2} />,
  Bash: <IconTerminal size={12} strokeWidth={2} />,
  Read: <IconFile size={12} strokeWidth={2} />,
  Write: <IconPencil size={12} strokeWidth={2} />,
  Edit: <IconPencil size={12} strokeWidth={2} />,
  apply_patch: <IconPlus size={12} strokeWidth={2} />,
  WebFetch: <IconArrowUpRight size={12} strokeWidth={2} />,
  WebSearch: <IconGlobe size={12} strokeWidth={2} />,
  Grep: <IconSearch size={12} strokeWidth={2} />,
  Glob: <IconSearch size={12} strokeWidth={2} />,
  TodoWrite: <IconCheck size={12} strokeWidth={2} />,
};

function toolIcon(fn: string): React.ReactNode {
  return TOOL_ICONS[fn] ?? <IconDotFilled size={6} />;
}

function formatTime(ts: number | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function previewText(text: string, max = 140): string {
  if (!text) return "";
  const stripped = text
    .replace(/\n+/g, " ")
    .replace(/[#*`>_~|]+/g, "")
    .trim();
  return stripped.length > max ? stripped.slice(0, max - 1) + "…" : stripped;
}

function summarizeOutput(out: string, more: (n: number) => string): string {
  if (!out) return "";
  const lines = out.trimEnd().split("\n");
  const first = lines[0] || "";
  if (lines.length === 1) return first.length > 100 ? first.slice(0, 99) + "…" : first;
  return `${first.slice(0, 80)}${first.length > 80 ? "…" : ""} · ${more(lines.length - 1)}`;
}

export function collapsedTextFor(summary: string, output: string): string {
  if (summary) return summary;
  if (!output) return "";
  const firstLine = output
    .split("\n")
    .find((line) => line.trim().length > 0)
    ?.trim();
  return firstLine ?? "";
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / 1024 / 1024).toFixed(1)}MB`;
}

interface Filters {
  responses: boolean;
  tools: boolean;
  user: boolean;
  system: boolean;
}

const DEFAULT_FILTERS: Filters = {
  responses: true,
  tools: true,
  user: true,
  system: false,
};

type TabId = "overview" | "files" | "commands" | "errors" | "conversation";
const TAB_ORDER: TabId[] = ["overview", "files", "commands", "errors", "conversation"];

interface FileChange {
  path: string;
  ops: { read: number; write: number; edit: number; other: number };
}

interface CommandSummary {
  cmd: string;
  count: number;
  failed: number;
  totalBytes: number;
}

function isReadTool(fn: string): boolean {
  return /Read|read_file|cat|sed|head|tail|nl|less|more|ls/i.test(fn);
}
function isWriteTool(fn: string): boolean {
  return /Write|write_file|apply_patch/i.test(fn);
}
function isEditTool(fn: string): boolean {
  return /Edit|patch/i.test(fn);
}

export function pathFromArgs(args: Record<string, unknown>, summary: string): string[] {
  const out: string[] = [];
  if (args.file_path) out.push(String(args.file_path));
  if (args.path) out.push(String(args.path));
  if (args.changes && Array.isArray(args.changes)) {
    for (const c of args.changes) {
      if (c && typeof c === "object" && "path" in c) out.push(String((c as { path: string }).path));
    }
  }
  // Try to extract paths from `cmd`/`command` like `sed -n '1,220p' /path/to/file`
  if (out.length === 0 && (args.cmd || args.command || summary)) {
    const text = String(args.cmd || args.command || summary);
    const pathMatch = text.match(/(?:^|\s)(\/[^\s'"`)]+(?:\.\w+)?)/g);
    if (pathMatch) {
      for (const p of pathMatch) out.push(p.trim());
    }
  }
  return out;
}

/** The run's known file surface for one branch's messages — same source
 * (`pathFromArgs` over tool-call args) the "top files" panel already uses,
 * reused here for file-link resolution so both stay in lockstep. */
export function extractFilePaths(messages: RunMessage[]): string[] {
  const toolMessages = messages.filter((m) => m.role === "tool_call" || m.role === "action");
  const paths = new Set<string>();
  for (const t of toolMessages) {
    const args = (t.arguments as Record<string, unknown>) ?? {};
    for (const p of pathFromArgs(args, t.summary || "")) paths.add(p);
  }
  return Array.from(paths);
}

function RunStepCard({
  step,
  defaultExpanded = false,
  expanded: expandedProp,
  onToggleExpand,
  runId,
  artifactRoot,
  runFiles,
}: RunStepCardProps) {
  const t = useTranslations("runCard");
  const [internalExpanded, setInternalExpanded] = useState(defaultExpanded);
  const isControlled = expandedProp !== undefined;
  const expanded = isControlled ? expandedProp : internalExpanded;
  const setExpanded = (next: boolean | ((prev: boolean) => boolean)) => {
    const resolved = typeof next === "function" ? next(expanded) : next;
    if (!isControlled) setInternalExpanded(resolved);
    onToggleExpand?.(step.step, resolved);
  };
  const [tab, setTab] = useState<TabId>("overview");
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [expandedTools, setExpandedTools] = useState<Set<number>>(new Set());

  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const handleTabKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLButtonElement>) => {
      const activeIndex = TAB_ORDER.indexOf(tab);
      let nextIndex: number | null = null;
      if (e.key === "ArrowRight") {
        nextIndex = (activeIndex + 1) % TAB_ORDER.length;
      } else if (e.key === "ArrowLeft") {
        nextIndex = (activeIndex - 1 + TAB_ORDER.length) % TAB_ORDER.length;
      } else if (e.key === "Home") {
        nextIndex = 0;
      } else if (e.key === "End") {
        nextIndex = TAB_ORDER.length - 1;
      }
      if (nextIndex !== null) {
        e.preventDefault();
        setTab(TAB_ORDER[nextIndex]);
        tabRefs.current[nextIndex]?.focus();
      }
    },
    [tab],
  );

  const messages = useMemo(() => step.messages ?? [], [step.messages]);
  const result = (step.result ?? {}) as StepResult;

  const counts = useMemo(() => {
    const c = { system: 0, user: 0, assistant: 0, tool_call: 0, action: 0 };
    for (const m of messages) {
      const r = m.role as keyof typeof c;
      if (r in c) c[r] = (c[r] || 0) + 1;
    }
    return c;
  }, [messages]);

  const lastAssistant = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "assistant") return messages[i];
    }
    return null;
  }, [messages]);

  const assistantList = useMemo(() => messages.filter((m) => m.role === "assistant"), [messages]);

  // ── Workflow summary: derive monitoring data ───────────────────────────
  const summary = useMemo(() => {
    const toolMessages = messages.filter((m) => m.role === "tool_call" || m.role === "action");
    const failedTools = toolMessages.filter((m) => m.status === "error");

    // File aggregation
    const fileMap = new Map<string, FileChange>();
    for (const t of toolMessages) {
      const args = (t.arguments as Record<string, unknown>) ?? {};
      const fn = t.function || "";
      const paths = pathFromArgs(args, t.summary || "");
      for (const p of paths) {
        if (!fileMap.has(p)) {
          fileMap.set(p, { path: p, ops: { read: 0, write: 0, edit: 0, other: 0 } });
        }
        const fc = fileMap.get(p)!;
        if (isWriteTool(fn)) fc.ops.write += 1;
        else if (isEditTool(fn)) fc.ops.edit += 1;
        else if (isReadTool(fn)) fc.ops.read += 1;
        else fc.ops.other += 1;
      }
    }

    // Command aggregation (by tool function name)
    const cmdMap = new Map<string, CommandSummary>();
    for (const t of toolMessages) {
      const fn = t.function || "tool";
      if (!cmdMap.has(fn)) {
        cmdMap.set(fn, { cmd: fn, count: 0, failed: 0, totalBytes: 0 });
      }
      const cs = cmdMap.get(fn)!;
      cs.count += 1;
      if (t.status === "error") cs.failed += 1;
      cs.totalBytes += (t.output || "").length;
    }

    // Duration: first → last timestamp
    let firstTs: number | null = null;
    let lastTs: number | null = null;
    for (const m of messages) {
      if (m.timestamp == null) continue;
      if (firstTs == null) firstTs = m.timestamp;
      lastTs = m.timestamp;
    }
    const durationSec =
      typeof result.duration_sec === "number"
        ? result.duration_sec
        : firstTs != null && lastTs != null
          ? Math.round(lastTs - firstTs)
          : null;

    return {
      toolCount: toolMessages.length,
      failedCount: failedTools.length,
      files: Array.from(fileMap.values()).sort((a, b) => {
        const aT = a.ops.read + a.ops.write + a.ops.edit + a.ops.other;
        const bT = b.ops.read + b.ops.write + b.ops.edit + b.ops.other;
        return bT - aT;
      }),
      commands: Array.from(cmdMap.values()).sort((a, b) => b.count - a.count),
      failedTools,
      durationSec,
      firstTs,
      lastTs,
    };
  }, [messages]);

  // File-link resolution context (shared by the overview + conversation
  // Markdown renderers): agent dir first, then the run-wide file surface.
  const fileContext = useMemo<FileResolutionContext | undefined>(() => {
    if (!runId) return undefined;
    const agentId = result.agent || step.step;
    const agentDir =
      artifactRoot && agentId ? `${artifactRoot.replace(/\/+$/, "")}/${agentId}` : undefined;
    const knownFiles = Array.from(
      new Set([...summary.files.map((f) => f.path), ...(runFiles ?? [])]),
    );
    return { runId, knownFiles, agentDir };
  }, [runId, artifactRoot, runFiles, result.agent, step.step, summary.files]);

  const tone = STATUS_TONE[step.status] ?? "pending";

  const toggleTool = (idx: number) => {
    setExpandedTools((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };

  return (
    <div
      id={`step-${step.step}`}
      className={`rounded-lg border bg-surface-base transition-colors ${
        step.status === "completed"
          ? "border-edge"
          : step.status === "failed"
            ? "border-status-error/40"
            : step.status === "running"
              ? "border-status-running/40"
              : "border-edge-subtle"
      }`}
    >
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={`step-${step.step}-body`}
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-surface-overlay"
      >
        <span className="mt-1 flex items-center text-content-muted">
          {expanded ? (
            <IconChevronDown size={10} strokeWidth={2.25} />
          ) : (
            <IconChevronRight size={10} strokeWidth={2.25} />
          )}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono text-label font-semibold text-content-primary">
              {step.step}
            </span>
            <Badge tone={tone}>{step.status}</Badge>
            {result.agent && (
              <span className="rounded-full bg-surface-overlay px-1.5 py-0 text-meta text-content-secondary">
                {result.agent}
              </span>
            )}
            {result.model && (
              <span className="font-mono text-meta text-content-muted">{result.model}</span>
            )}
            <span className="ml-auto flex items-center gap-2 font-mono text-meta text-content-muted">
              <span>{t("countTools", { count: summary.toolCount })}</span>
              {summary.failedCount > 0 && (
                <span className="text-status-error">
                  {t("countFailed", { count: summary.failedCount })}
                </span>
              )}
              <span>{t("countFiles", { count: summary.files.length })}</span>
              {summary.durationSec != null && (
                <span>
                  {summary.durationSec < 60
                    ? `${summary.durationSec}s`
                    : `${Math.floor(summary.durationSec / 60)}m ${summary.durationSec % 60}s`}
                </span>
              )}
            </span>
          </div>
          {!expanded && lastAssistant && (
            <p className="mt-0.5 text-body text-content-muted leading-snug">
              <span className="text-content-muted">→ </span>
              {previewText(lastAssistant.content || "", 220)}
            </p>
          )}
        </div>
      </button>

      {expanded && (
        <div id={`step-${step.step}-body`} className="border-t border-edge">
          {/* Tab bar */}
          <div
            role="tablist"
            aria-label={t("stepDetails")}
            className="sticky top-0 z-10 flex items-center gap-0 border-b border-edge bg-surface-base/95 px-2 backdrop-blur"
          >
            <TabButton
              id="overview"
              active={tab}
              onSelect={setTab}
              label={t("tabOverview")}
              panelId={`step-${step.step}-panel-overview`}
              buttonId={`step-${step.step}-tab-overview`}
              tabIndex={tab === "overview" ? 0 : -1}
              ref={(el) => {
                tabRefs.current[0] = el;
              }}
              onKeyDown={handleTabKeyDown}
            />
            <TabButton
              id="files"
              active={tab}
              onSelect={setTab}
              label={t("tabFiles")}
              count={summary.files.length}
              panelId={`step-${step.step}-panel-files`}
              buttonId={`step-${step.step}-tab-files`}
              tabIndex={tab === "files" ? 0 : -1}
              ref={(el) => {
                tabRefs.current[1] = el;
              }}
              onKeyDown={handleTabKeyDown}
            />
            <TabButton
              id="commands"
              active={tab}
              onSelect={setTab}
              label={t("tabCommands")}
              count={summary.toolCount}
              panelId={`step-${step.step}-panel-commands`}
              buttonId={`step-${step.step}-tab-commands`}
              tabIndex={tab === "commands" ? 0 : -1}
              ref={(el) => {
                tabRefs.current[2] = el;
              }}
              onKeyDown={handleTabKeyDown}
            />
            <TabButton
              id="errors"
              active={tab}
              onSelect={setTab}
              label={t("tabErrors")}
              count={summary.failedCount}
              tone={summary.failedCount > 0 ? "error" : undefined}
              panelId={`step-${step.step}-panel-errors`}
              buttonId={`step-${step.step}-tab-errors`}
              tabIndex={tab === "errors" ? 0 : -1}
              ref={(el) => {
                tabRefs.current[3] = el;
              }}
              onKeyDown={handleTabKeyDown}
            />
            <TabButton
              id="conversation"
              active={tab}
              onSelect={setTab}
              label={t("tabConversation")}
              count={messages.length}
              panelId={`step-${step.step}-panel-conversation`}
              buttonId={`step-${step.step}-tab-conversation`}
              tabIndex={tab === "conversation" ? 0 : -1}
              ref={(el) => {
                tabRefs.current[4] = el;
              }}
              onKeyDown={handleTabKeyDown}
            />
            {tab === "conversation" && assistantList.length > 0 && (
              <button
                type="button"
                onClick={() => {
                  const all = new Set<number>();
                  messages.forEach((m, i) => {
                    if (m.role === "tool_call" || m.role === "action") all.add(i);
                  });
                  setExpandedTools(expandedTools.size > 0 ? new Set() : all);
                }}
                className="ml-auto rounded border border-edge px-2 py-0.5 text-[length:var(--t-xs)] text-content-muted hover:border-edge-strong hover:text-content-primary"
              >
                {expandedTools.size > 0 ? t("collapseAllTools") : t("expandAllTools")}
              </button>
            )}
          </div>

          {tab === "overview" && (
            <div
              role="tabpanel"
              id={`step-${step.step}-panel-overview`}
              aria-labelledby={`step-${step.step}-tab-overview`}
            >
              <OverviewPanel
                summary={summary}
                lastAssistant={lastAssistant}
                onJumpToConversation={() => setTab("conversation")}
                fileContext={fileContext}
              />
            </div>
          )}

          {tab === "files" && (
            <div
              role="tabpanel"
              id={`step-${step.step}-panel-files`}
              aria-labelledby={`step-${step.step}-tab-files`}
            >
              <FilesPanel files={summary.files} />
            </div>
          )}

          {tab === "commands" && (
            <div
              role="tabpanel"
              id={`step-${step.step}-panel-commands`}
              aria-labelledby={`step-${step.step}-tab-commands`}
            >
              <CommandsPanel commands={summary.commands} />
            </div>
          )}

          {tab === "errors" && (
            <div
              role="tabpanel"
              id={`step-${step.step}-panel-errors`}
              aria-labelledby={`step-${step.step}-tab-errors`}
            >
              <ErrorsPanel failed={summary.failedTools} />
            </div>
          )}

          {tab === "conversation" && (
            <div
              role="tabpanel"
              id={`step-${step.step}-panel-conversation`}
              aria-labelledby={`step-${step.step}-tab-conversation`}
            >
              <div className="flex flex-wrap items-center gap-1.5 border-b border-edge px-2 py-1">
                <span className="text-[length:var(--t-xs)] uppercase tracking-wide text-content-muted">
                  {t("filterLabel")}
                </span>
                <FilterChip
                  label={t("filterResponses")}
                  count={counts.assistant}
                  active={filters.responses}
                  tone="blue"
                  onToggle={() => setFilters((f) => ({ ...f, responses: !f.responses }))}
                />
                <FilterChip
                  label={t("filterTools")}
                  count={counts.tool_call + counts.action}
                  active={filters.tools}
                  tone="amber"
                  onToggle={() => setFilters((f) => ({ ...f, tools: !f.tools }))}
                />
                <FilterChip
                  label={t("filterUser")}
                  count={counts.user}
                  active={filters.user}
                  tone="green"
                  onToggle={() => setFilters((f) => ({ ...f, user: !f.user }))}
                />
                <FilterChip
                  label={t("filterSystem")}
                  count={counts.system}
                  active={filters.system}
                  tone="neutral"
                  onToggle={() => setFilters((f) => ({ ...f, system: !f.system }))}
                />
              </div>
              <MessageFeed
                messages={messages}
                filters={filters}
                expandedTools={expandedTools}
                onToggleTool={toggleTool}
                stepKey={step.step}
                fileContext={fileContext}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Memoized so a live session that appends to one branch only re-renders that
// branch's card. The parent rebuilds every step object each tick, so we compare
// by content rather than identity: messages are usually append-only, but a
// tool call's `output`/`status` can be patched in place on an existing message
// slot (paired action-response merge), so length alone would miss that update.
export function runMessageMemoKey(message: RunMessage): string {
  return [
    message.role ?? "",
    message.content ?? "",
    message.timestamp ?? "",
    message.function ?? "",
    message.summary ?? "",
    JSON.stringify(message.arguments ?? null),
    message.output ?? "",
    message.status ?? "",
    message.exit_code ?? "",
  ].join("");
}

export function runMessagesEqualForMemo(
  prev: RunMessage[] | undefined,
  next: RunMessage[] | undefined,
): boolean {
  const prevMessages = prev ?? [];
  const nextMessages = next ?? [];
  if (prevMessages.length !== nextMessages.length) return false;
  for (let i = 0; i < prevMessages.length; i++) {
    if (runMessageMemoKey(prevMessages[i]) !== runMessageMemoKey(nextMessages[i])) return false;
  }
  return true;
}

export function stepPropsEqual(prev: RunStepCardProps, next: RunStepCardProps): boolean {
  const prevResult = (prev.step.result ?? {}) as StepResult;
  const nextResult = (next.step.result ?? {}) as StepResult;
  return (
    prev.expanded === next.expanded &&
    prev.defaultExpanded === next.defaultExpanded &&
    prev.onToggleExpand === next.onToggleExpand &&
    prev.step.step === next.step.step &&
    prev.step.status === next.step.status &&
    prev.step.timestamp === next.step.timestamp &&
    prevResult.agent === nextResult.agent &&
    prevResult.model === nextResult.model &&
    prevResult.message_count === nextResult.message_count &&
    prevResult.duration_sec === nextResult.duration_sec &&
    prev.runId === next.runId &&
    prev.artifactRoot === next.artifactRoot &&
    prev.runFiles === next.runFiles &&
    runMessagesEqualForMemo(prev.step.messages, next.step.messages)
  );
}

export default React.memo(RunStepCard, stepPropsEqual);

const TabButton = React.forwardRef<
  HTMLButtonElement,
  {
    id: TabId;
    active: TabId;
    onSelect: (id: TabId) => void;
    label: string;
    count?: number;
    tone?: "error";
    panelId: string;
    buttonId: string;
    tabIndex?: number;
    onKeyDown?: (e: React.KeyboardEvent<HTMLButtonElement>) => void;
  }
>(function TabButton(
  { id, active, onSelect, label, count, tone, panelId, buttonId, tabIndex, onKeyDown },
  ref,
) {
  const t = useTranslations("runCard");
  const isActive = id === active;
  const tabPosition = TAB_ORDER.indexOf(id) + 1;
  const totalTabs = TAB_ORDER.length;
  return (
    <button
      ref={ref}
      type="button"
      id={buttonId}
      role="tab"
      aria-selected={isActive}
      aria-controls={panelId}
      aria-label={t("tabAria", { label, position: tabPosition, total: totalTabs })}
      tabIndex={tabIndex ?? (isActive ? 0 : -1)}
      onClick={() => onSelect(id)}
      onKeyDown={onKeyDown}
      className={`relative -mb-px flex items-center gap-1.5 border-b-2 px-3 py-1.5 text-body font-medium transition-colors ${
        isActive
          ? "border-status-running text-content-primary"
          : "border-transparent text-content-muted hover:text-content-secondary"
      }`}
    >
      {label}
      {count != null && (
        <span
          className={`rounded px-1 font-mono text-[length:var(--t-xs)] ${tone === "error" ? "bg-status-error-bg text-status-error" : "bg-surface-overlay text-content-muted"}`}
        >
          {count}
        </span>
      )}
    </button>
  );
});

function OverviewPanel({
  summary,
  lastAssistant,
  onJumpToConversation,
  fileContext,
}: {
  summary: {
    toolCount: number;
    failedCount: number;
    files: FileChange[];
    commands: CommandSummary[];
    failedTools: RunMessage[];
    durationSec: number | null;
  };
  lastAssistant: RunMessage | null;
  onJumpToConversation: () => void;
  fileContext?: FileResolutionContext;
}) {
  const t = useTranslations("runCard");
  return (
    <div className="grid grid-cols-1 gap-2 p-2 lg:grid-cols-3">
      <div className="lg:col-span-2 rounded border border-edge bg-surface-raised p-3">
        <div className="mb-1.5 flex items-center gap-2">
          <span className="text-[length:var(--t-xs)] font-semibold uppercase tracking-wider text-content-muted">
            {t("latestMessage")}
          </span>
        </div>
        {lastAssistant?.content ? (
          <>
            <Suspense fallback={null}>
              <Markdown className="text-body leading-snug" fileContext={fileContext}>
                {lastAssistant.content.length > 1200
                  ? lastAssistant.content.slice(0, 1200) + "\n\n…"
                  : lastAssistant.content}
              </Markdown>
            </Suspense>
            {lastAssistant.content.length > 1200 && (
              <button
                type="button"
                onClick={onJumpToConversation}
                className="mt-2 text-meta text-status-running hover:text-status-running/80 transition-colors"
              >
                {t("viewFullConversation")}
              </button>
            )}
          </>
        ) : (
          <p className="text-body text-content-muted">{t("noFinalResponse")}</p>
        )}
      </div>
      <div className="flex flex-col gap-2">
        <StatCard
          label={t("statToolCalls")}
          value={summary.toolCount.toString()}
          sub={t("kindsSub", { count: summary.commands.length })}
        />
        <StatCard
          label={t("statFailed")}
          value={summary.failedCount.toString()}
          tone={summary.failedCount > 0 ? "error" : "ok"}
        />
        <StatCard label={t("statFilesTouched")} value={summary.files.length.toString()} />
        {summary.durationSec != null && (
          <StatCard
            label={t("statDuration")}
            value={
              summary.durationSec < 60
                ? `${summary.durationSec}s`
                : `${Math.floor(summary.durationSec / 60)}m ${summary.durationSec % 60}s`
            }
          />
        )}
      </div>
      {summary.commands.length > 0 && (
        <div className="rounded border border-edge bg-surface-raised p-2">
          <div className="mb-1.5 text-[length:var(--t-xs)] font-semibold uppercase tracking-wider text-content-muted">
            {t("topCommands")}
          </div>
          <ul className="flex flex-col gap-0.5">
            {summary.commands.slice(0, 8).map((c) => (
              <li key={c.cmd} className="flex items-center justify-between gap-2 text-body">
                <span className="truncate font-mono text-status-warning">{c.cmd}</span>
                <span className="shrink-0 font-mono text-meta text-content-muted">
                  ×{c.count}
                  {c.failed > 0 && (
                    <span className="text-status-error"> {t("errCount", { count: c.failed })}</span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {summary.files.length > 0 && (
        <div className="rounded border border-edge bg-surface-raised p-2 lg:col-span-2">
          <div className="mb-1.5 text-[length:var(--t-xs)] font-semibold uppercase tracking-wider text-content-muted">
            {t("topFiles")}
          </div>
          <ul className="flex flex-col gap-0.5">
            {summary.files.slice(0, 8).map((f) => (
              <FileRow key={f.path} file={f} />
            ))}
          </ul>
        </div>
      )}
      {summary.failedCount > 0 && (
        <div className="rounded border border-status-error/30 bg-status-error-bg p-2 lg:col-span-3">
          <div className="mb-1.5 text-[length:var(--t-xs)] font-semibold uppercase tracking-wider text-status-error">
            {t("failedToolCalls", { count: summary.failedCount })}
          </div>
          <ul className="flex flex-col gap-1">
            {summary.failedTools.slice(0, 5).map((t, i) => (
              <li key={i} className="text-body">
                <span className="font-mono text-status-error">{t.function}</span>
                <span className="ml-2 truncate font-mono text-content-secondary">{t.summary}</span>
                {t.exit_code != null && (
                  <span className="ml-2 text-meta text-status-error">exit {t.exit_code}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function FileRow({ file }: { file: FileChange }) {
  const ops = file.ops;
  const total = ops.read + ops.write + ops.edit + ops.other;
  return (
    <li className="flex items-center justify-between gap-2 text-body">
      <span className="truncate font-mono text-content-secondary" title={file.path}>
        {file.path.length > 60 ? "…" + file.path.slice(-60) : file.path}
      </span>
      <span className="shrink-0 flex items-center gap-1 font-mono text-meta">
        {ops.read > 0 && <span className="text-status-running">r{ops.read}</span>}
        {ops.edit > 0 && <span className="text-status-warning">e{ops.edit}</span>}
        {ops.write > 0 && <span className="text-status-success">w{ops.write}</span>}
        {ops.other > 0 && <span className="text-content-muted">·{ops.other}</span>}
        <span className="ml-1 text-content-muted">({total})</span>
      </span>
    </li>
  );
}

function FilesPanel({ files }: { files: FileChange[] }) {
  const t = useTranslations("runCard");
  if (files.length === 0)
    return <div className="p-4 text-body text-content-muted">{t("noFileActivity")}</div>;
  return (
    <div className="p-2">
      <ul className="flex flex-col gap-0.5">
        {files.map((f) => (
          <FileRow key={f.path} file={f} />
        ))}
      </ul>
    </div>
  );
}

function CommandsPanel({ commands }: { commands: CommandSummary[] }) {
  const t = useTranslations("runCard");
  if (commands.length === 0)
    return <div className="p-4 text-body text-content-muted">{t("noCommands")}</div>;
  return (
    <div className="p-2">
      <table className="w-full text-left text-body">
        <thead>
          <tr className="border-b border-edge text-[length:var(--t-xs)] uppercase tracking-wider text-content-muted">
            <th className="px-2 py-1 font-medium">{t("colTool")}</th>
            <th className="px-2 py-1 text-right font-medium">{t("colCalls")}</th>
            <th className="px-2 py-1 text-right font-medium">{t("colFailed")}</th>
            <th className="px-2 py-1 text-right font-medium">{t("colOutput")}</th>
          </tr>
        </thead>
        <tbody>
          {commands.map((c) => (
            <tr key={c.cmd} className="border-b border-edge-subtle">
              <td className="px-2 py-1 font-mono text-status-warning">{c.cmd}</td>
              <td className="px-2 py-1 text-right font-mono text-content-primary">{c.count}</td>
              <td
                className={`px-2 py-1 text-right font-mono ${c.failed > 0 ? "text-status-error" : "text-content-muted"}`}
              >
                {c.failed > 0 ? c.failed : "—"}
              </td>
              <td className="px-2 py-1 text-right font-mono text-content-muted">
                {formatBytes(c.totalBytes)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ErrorsPanel({ failed }: { failed: RunMessage[] }) {
  const t = useTranslations("runCard");
  if (failed.length === 0)
    return <div className="p-4 text-body text-status-success">{t("noErrors")}</div>;
  return (
    <div className="flex flex-col gap-1.5 p-2">
      {failed.map((t, i) => (
        <div key={i} className="rounded border border-status-error/30 bg-status-error-bg p-2">
          <div className="flex items-center gap-2 text-body">
            <span className="font-mono text-status-error">{t.function}</span>
            {t.exit_code != null && (
              <span className="rounded bg-status-error-bg border border-status-error/30 px-1.5 py-0 font-mono text-meta text-status-error">
                exit {t.exit_code}
              </span>
            )}
            <span className="ml-auto font-mono text-meta text-content-muted">
              {t.timestamp ? formatTime(t.timestamp) : ""}
            </span>
          </div>
          <p
            className="mt-0.5 truncate font-mono text-body text-content-secondary"
            title={t.summary}
          >
            $ {t.summary}
          </p>
          {t.output && (
            <pre className="mt-1.5 max-h-40 overflow-auto rounded bg-status-error-bg border border-status-error/20 p-1.5 font-mono text-meta leading-relaxed text-status-error">
              {t.output.length > 2000 ? t.output.slice(0, 2000) + "\n…[truncated]" : t.output}
            </pre>
          )}
        </div>
      ))}
    </div>
  );
}

interface MessageFeedProps {
  messages: RunMessage[];
  filters: Filters;
  expandedTools: Set<number>;
  onToggleTool: (idx: number) => void;
  stepKey?: string;
  fileContext?: FileResolutionContext;
}

const MESSAGE_WINDOW = 80;

function MessageFeed({
  messages,
  filters,
  expandedTools,
  onToggleTool,
  stepKey = "",
  fileContext,
}: MessageFeedProps) {
  const t = useTranslations("runCard");
  // Render only the most recent window; long conversations reveal older turns
  // on demand instead of mounting the whole history at once.
  const [visibleCount, setVisibleCount] = useState(MESSAGE_WINDOW);

  // Precompute per-message assistant ordinals before JSX to avoid mutation during render
  const assistantOrdinals: number[] = new Array(messages.length);
  let ordinalCount = 0;
  for (let i = 0; i < messages.length; i++) {
    if (messages[i].role === "assistant") ordinalCount += 1;
    assistantOrdinals[i] = ordinalCount;
  }

  const startIdx = Math.max(0, messages.length - visibleCount);

  return (
    <div className="flex flex-col">
      {startIdx > 0 && (
        <button
          type="button"
          onClick={() => setVisibleCount((v) => v + MESSAGE_WINDOW * 4)}
          className="border-b border-edge px-3 py-1.5 text-center font-mono text-[length:var(--t-xs)] text-content-muted hover:bg-surface-overlay hover:text-content-secondary"
        >
          {t("showEarlier", { count: startIdx })}
        </button>
      )}
      {messages.slice(startIdx).map((m, offset) => {
        const i = startIdx + offset;
        if (m.role === "system" && !filters.system) return null;
        if (m.role === "user" && !filters.user) return null;
        if (m.role === "assistant" && !filters.responses) return null;
        if ((m.role === "tool_call" || m.role === "action") && !filters.tools) return null;

        if (m.role === "system") {
          return <SystemBlock key={i} content={m.content || ""} />;
        }
        if (m.role === "user") {
          return <UserBlock key={i} content={m.content || ""} timestamp={m.timestamp} />;
        }
        if (m.role === "assistant") {
          const ordinal = assistantOrdinals[i];
          return (
            <AssistantBlock
              key={i}
              anchorId={`step-${stepKey}-r${ordinal - 1}`}
              content={m.content || ""}
              timestamp={m.timestamp}
              ordinal={ordinal}
              fileContext={fileContext}
            />
          );
        }
        if (m.role === "tool_call" || m.role === "action") {
          return (
            <ToolCallBlock
              key={i}
              message={m}
              expanded={expandedTools.has(i)}
              onToggle={() => onToggleTool(i)}
            />
          );
        }
        return null;
      })}
    </div>
  );
}

function SystemBlock({ content }: { content: string }) {
  const t = useTranslations("runCard");
  return (
    <details className="border-b border-edge">
      <summary className="cursor-pointer px-4 py-1.5 text-[length:var(--t-xs)] text-content-muted hover:bg-surface-overlay hover:text-content-secondary">
        <span className="font-mono uppercase tracking-wide">system</span>{" "}
        <span className="text-content-muted">{t("charsCount", { count: content.length })}</span>
      </summary>
      <div className="bg-surface-base px-4 py-2">
        <p className="max-h-48 overflow-y-auto whitespace-pre-wrap font-mono text-[length:var(--t-xs)] leading-relaxed text-content-secondary">
          {content}
        </p>
      </div>
    </details>
  );
}

function UserBlock({ content, timestamp }: { content: string; timestamp?: number | null }) {
  const t = useTranslations("runCard");
  const [open, setOpen] = useState(content.length < 200);
  return (
    <div className="border-b border-edge border-l-2 border-l-status-success bg-surface-overlay/40 px-3 py-1.5">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-2 text-left"
      >
        <span className="shrink-0 text-[length:var(--t-xs)] font-medium uppercase tracking-wide text-status-success">
          {t("roleUser")}
        </span>
        {!open && (
          <span className="min-w-0 truncate text-body text-content-secondary">
            {previewText(content, 200)}
          </span>
        )}
        {timestamp && open && (
          <span className="ml-auto shrink-0 text-[length:var(--t-xs)] text-content-muted">
            {formatTime(timestamp)}
          </span>
        )}
      </button>
      {open && (
        <p className="mt-1 whitespace-pre-wrap break-words text-body leading-snug text-content-secondary">
          {content}
        </p>
      )}
    </div>
  );
}

function AssistantBlock({
  anchorId,
  content,
  timestamp,
  ordinal,
  fileContext,
}: {
  anchorId: string;
  content: string;
  timestamp?: number | null;
  ordinal: number;
  fileContext?: FileResolutionContext;
}) {
  const t = useTranslations("runCard");
  const isThinking = content.startsWith("[thinking]");
  const displayText = isThinking ? content.replace(/^\[thinking\]\s*/, "") : content;
  return (
    <div
      id={anchorId}
      className={`border-b border-edge border-l-2 px-3 py-2 ${
        isThinking
          ? "border-l-edge-strong bg-surface-base"
          : "border-l-status-running bg-status-running-bg"
      }`}
    >
      <div className="mb-1 flex items-center gap-2">
        <span
          className={`shrink-0 rounded px-1 font-mono text-[length:var(--t-xs)] ${isThinking ? "bg-surface-overlay text-content-muted" : "bg-status-running-bg border border-status-running/30 text-status-running"}`}
        >
          #{ordinal}
        </span>
        <span
          className={`text-[length:var(--t-xs)] font-medium uppercase tracking-wide ${
            isThinking ? "text-content-muted" : "text-status-running"
          }`}
        >
          {isThinking ? t("roleThinking") : t("roleResponse")}
        </span>
        {timestamp && (
          <span className="ml-auto text-[length:var(--t-xs)] text-content-muted">
            {formatTime(timestamp)}
          </span>
        )}
      </div>
      {isThinking ? (
        <p className="whitespace-pre-wrap break-words text-body leading-snug text-content-muted italic">
          {displayText}
        </p>
      ) : (
        <Suspense fallback={null}>
          <Markdown className="text-body leading-snug" fileContext={fileContext}>
            {displayText}
          </Markdown>
        </Suspense>
      )}
    </div>
  );
}

function ToolCallBlock({
  message,
  expanded,
  onToggle,
}: {
  message: RunMessage;
  expanded: boolean;
  onToggle: () => void;
}) {
  const t = useTranslations("runCard");
  const fn = message.function || "tool";
  const summary = message.summary || "";
  const output = message.output || "";
  const status = message.status || "ok";
  const exitCode = message.exit_code;
  const isError = status === "error";
  // Collapsed-row fallback: an empty summary with non-empty output shows the
  // output's first non-blank line instead of a bare "(no args)" — that line
  // is usually more informative than a static placeholder.
  const collapsedText = collapsedTextFor(summary, output);

  const statusBadge = isError ? (
    <span className="rounded border border-status-error/30 bg-status-error-bg px-1.5 py-0.5 text-[length:var(--t-xs)] font-medium text-status-error">
      {exitCode != null ? `exit ${exitCode}` : "ERR"}
    </span>
  ) : (
    <span className="inline-flex items-center rounded border border-status-success/30 bg-status-success-bg px-1.5 py-1 text-[length:var(--t-xs)] font-medium text-status-success">
      <IconCheck size={10} strokeWidth={2.5} />
    </span>
  );

  return (
    <div className={`border-b border-edge ${isError ? "bg-status-error-bg" : "bg-surface-base"}`}>
      <button
        type="button"
        aria-expanded={expanded}
        onClick={onToggle}
        className="flex w-full items-center gap-2 px-3 py-0.5 text-left hover:bg-surface-overlay"
      >
        <span className="flex w-4 shrink-0 items-center justify-center text-status-warning">
          {toolIcon(fn)}
        </span>
        <span className="rounded border border-status-warning/30 bg-status-warning-bg px-1.5 py-0.5 font-mono text-meta text-status-warning">
          {fn}
        </span>
        <span
          className="flex-1 truncate font-mono text-body text-content-secondary"
          title={collapsedText || undefined}
        >
          {collapsedText || t("noArgs")}
        </span>
        {statusBadge}
        {output && (
          <span className="text-meta text-content-muted">{formatBytes(output.length)}</span>
        )}
        <span className="flex items-center text-content-muted">
          {expanded ? (
            <IconChevronDown size={9} strokeWidth={2.25} />
          ) : (
            <IconChevronRight size={9} strokeWidth={2.25} />
          )}
        </span>
      </button>

      {expanded && (
        <div className="border-t border-edge bg-surface-raised px-4 py-2.5">
          {message.arguments && Object.keys(message.arguments).length > 1 && (
            <div className="mb-3">
              <div className="mb-1 text-[length:var(--t-xs)] uppercase tracking-wide text-content-muted">
                {t("argsChip")}
              </div>
              <pre className="max-h-64 overflow-auto rounded bg-surface-overlay p-2 font-mono text-[length:var(--t-xs)] leading-relaxed text-content-secondary">
                {JSON.stringify(message.arguments, null, 2)}
              </pre>
            </div>
          )}
          <div>
            <div className="mb-1 flex items-center gap-2 text-[length:var(--t-xs)] uppercase tracking-wide text-content-muted">
              <span>{t("outputChip")}</span>
              <span className="text-content-muted">{formatBytes(output.length)}</span>
            </div>
            <pre
              className={`max-h-96 overflow-auto rounded p-2 font-mono text-meta leading-relaxed ${
                isError
                  ? "bg-status-error-bg text-status-error border border-status-error/20"
                  : "bg-surface-overlay text-content-secondary"
              }`}
            >
              {output || t("noOutput")}
            </pre>
          </div>
        </div>
      )}

      {!expanded && output && (
        <p className="ml-6 mr-4 pb-1 text-[length:var(--t-xs)] text-content-muted">
          {summarizeOutput(output, (n) => t("moreLines", { count: n }))}
        </p>
      )}
    </div>
  );
}
