import React, { lazy, Suspense, useCallback, useMemo, useRef, useState } from "react";
import Badge from "@/components/Badge";
import type { RunMessage, RunStep } from "@/lib/types";

const Markdown = lazy(() => import("@/components/Markdown"));

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
  roles?: RolesBreakdown;
  [key: string]: unknown;
}

export interface RunStepCardProps {
  step: RunStep;
  defaultExpanded?: boolean;
  expanded?: boolean;
  onToggleExpand?: (stepId: string, next: boolean) => void;
}

const STATUS_TONE: Record<string, "ok" | "pending" | "failed"> = {
  completed: "ok",
  running: "pending",
  failed: "failed",
};

const TOOL_ICONS: Record<string, string> = {
  exec_command: "$_",
  Bash: "$_",
  Read: "📄",
  Write: "✎",
  Edit: "✎",
  apply_patch: "⊕",
  WebFetch: "↗",
  WebSearch: "🔍",
  Grep: "/?",
  Glob: "**",
  TodoWrite: "☑",
};

function toolIcon(fn: string): string {
  return TOOL_ICONS[fn] || "•";
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

function summarizeOutput(out: string): string {
  if (!out) return "(no output)";
  const lines = out.trimEnd().split("\n");
  const first = lines[0] || "";
  if (lines.length === 1) return first.length > 100 ? first.slice(0, 99) + "…" : first;
  return `${first.slice(0, 80)}${first.length > 80 ? "…" : ""} · +${lines.length - 1} more`;
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

const VERDICT_RE =
  /\b(APPROVE-WITH-FIXES|APPROVE-WITH-SUGGESTIONS|APPROVE|REJECT|REQUEST CHANGES|PASS|FAIL|BLOCK)\b/i;

function extractVerdict(text: string): string | null {
  if (!text) return null;
  const m = text.match(VERDICT_RE);
  return m ? m[1].toUpperCase() : null;
}

const VERDICT_TONE: Record<string, "ok" | "pending" | "failed"> = {
  APPROVE: "ok",
  "APPROVE-WITH-SUGGESTIONS": "ok",
  "APPROVE-WITH-FIXES": "pending",
  PASS: "ok",
  REJECT: "failed",
  "REQUEST CHANGES": "failed",
  FAIL: "failed",
  BLOCK: "failed",
};

function isReadTool(fn: string): boolean {
  return /Read|read_file|cat|sed|head|tail|nl|less|more|ls/i.test(fn);
}
function isWriteTool(fn: string): boolean {
  return /Write|write_file|apply_patch/i.test(fn);
}
function isEditTool(fn: string): boolean {
  return /Edit|patch/i.test(fn);
}

function pathFromArgs(args: Record<string, unknown>, summary: string): string[] {
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

export default function RunStepCard({
  step,
  defaultExpanded = false,
  expanded: expandedProp,
  onToggleExpand,
}: RunStepCardProps) {
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
  const roles = (result.roles ?? {}) as RolesBreakdown;

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

    // Verdict from last assistant (or first that has a verdict pattern)
    let verdict: string | null = null;
    if (lastAssistant?.content) verdict = extractVerdict(lastAssistant.content);
    if (!verdict) {
      for (const a of assistantList) {
        const v = extractVerdict(a.content || "");
        if (v) {
          verdict = v;
          break;
        }
      }
    }

    // Duration: first → last timestamp
    let firstTs: number | null = null;
    let lastTs: number | null = null;
    for (const m of messages) {
      if (m.timestamp == null) continue;
      if (firstTs == null) firstTs = m.timestamp;
      lastTs = m.timestamp;
    }
    const durationSec = firstTs != null && lastTs != null ? Math.round(lastTs - firstTs) : null;

    return {
      verdict,
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
  }, [messages, lastAssistant, assistantList]);

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
        <span className="mt-0.5 text-body text-content-muted">{expanded ? "▾" : "▸"}</span>
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
            {summary.verdict && (
              <Badge
                tone={VERDICT_TONE[summary.verdict] ?? "pending"}
                className="rounded font-mono"
              >
                {summary.verdict}
              </Badge>
            )}
            <span className="ml-auto flex items-center gap-2 font-mono text-meta text-content-muted">
              <span>{summary.toolCount} tools</span>
              {summary.failedCount > 0 && (
                <span className="text-status-error">{summary.failedCount} failed</span>
              )}
              <span>{summary.files.length} files</span>
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
            aria-label="Step details"
            className="sticky top-0 z-10 flex items-center gap-0 border-b border-edge bg-surface-base/95 px-2 backdrop-blur"
          >
            <TabButton
              id="overview"
              active={tab}
              onSelect={setTab}
              label="Overview"
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
              label="Files"
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
              label="Commands"
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
              label="Errors"
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
              label="Conversation"
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
                className="ml-auto rounded border border-edge px-2 py-0.5 text-[10px] text-content-muted hover:border-edge-strong hover:text-content-primary"
              >
                {expandedTools.size > 0 ? "collapse all tools" : "expand all tools"}
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
                <span className="text-[9px] uppercase tracking-wide text-content-muted">
                  filter:
                </span>
                <FilterChip
                  label="responses"
                  count={counts.assistant}
                  active={filters.responses}
                  tone="blue"
                  onToggle={() => setFilters((f) => ({ ...f, responses: !f.responses }))}
                />
                <FilterChip
                  label="tools"
                  count={counts.tool_call + counts.action}
                  active={filters.tools}
                  tone="amber"
                  onToggle={() => setFilters((f) => ({ ...f, tools: !f.tools }))}
                />
                <FilterChip
                  label="user"
                  count={counts.user}
                  active={filters.user}
                  tone="green"
                  onToggle={() => setFilters((f) => ({ ...f, user: !f.user }))}
                />
                <FilterChip
                  label="system"
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
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

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
      aria-label={`${label} tab ${tabPosition} of ${totalTabs}`}
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
          className={`rounded px-1 font-mono text-[9px] ${tone === "error" ? "bg-status-error-bg text-status-error" : "bg-surface-overlay text-content-muted"}`}
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
}: {
  summary: {
    verdict: string | null;
    toolCount: number;
    failedCount: number;
    files: FileChange[];
    commands: CommandSummary[];
    failedTools: RunMessage[];
    durationSec: number | null;
  };
  lastAssistant: RunMessage | null;
  onJumpToConversation: () => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-2 p-2 lg:grid-cols-3">
      <div className="lg:col-span-2 rounded border border-edge bg-surface-raised p-3">
        <div className="mb-1.5 flex items-center gap-2">
          <span className="text-[9px] font-semibold uppercase tracking-wider text-content-muted">
            Outcome
          </span>
          {summary.verdict && (
            <Badge tone={VERDICT_TONE[summary.verdict] ?? "pending"} className="rounded font-mono">
              {summary.verdict}
            </Badge>
          )}
        </div>
        {lastAssistant?.content ? (
          <>
            <Suspense fallback={null}>
              <Markdown className="text-body leading-snug">
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
                View full conversation →
              </button>
            )}
          </>
        ) : (
          <p className="text-body text-content-muted">No final response recorded.</p>
        )}
      </div>
      <div className="flex flex-col gap-2">
        <StatBlock
          label="Tool calls"
          value={summary.toolCount.toString()}
          sub={`${summary.commands.length} kinds`}
        />
        <StatBlock
          label="Failed"
          value={summary.failedCount.toString()}
          tone={summary.failedCount > 0 ? "error" : "ok"}
        />
        <StatBlock label="Files touched" value={summary.files.length.toString()} />
        {summary.durationSec != null && (
          <StatBlock
            label="Duration"
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
          <div className="mb-1.5 text-[9px] font-semibold uppercase tracking-wider text-content-muted">
            Top Commands
          </div>
          <ul className="flex flex-col gap-0.5">
            {summary.commands.slice(0, 8).map((c) => (
              <li key={c.cmd} className="flex items-center justify-between gap-2 text-body">
                <span className="truncate font-mono text-status-warning">{c.cmd}</span>
                <span className="shrink-0 font-mono text-meta text-content-muted">
                  ×{c.count}
                  {c.failed > 0 && <span className="text-status-error"> ({c.failed} err)</span>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {summary.files.length > 0 && (
        <div className="rounded border border-edge bg-surface-raised p-2 lg:col-span-2">
          <div className="mb-1.5 text-[9px] font-semibold uppercase tracking-wider text-content-muted">
            Top Files
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
          <div className="mb-1.5 text-[9px] font-semibold uppercase tracking-wider text-status-error">
            {summary.failedCount} Failed Tool Call{summary.failedCount === 1 ? "" : "s"}
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

function StatBlock({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "ok" | "error";
}) {
  return (
    <div className="rounded border border-edge bg-surface-raised px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-content-muted">{label}</div>
      <div
        className={`mt-0.5 font-mono text-base font-semibold ${
          tone === "error"
            ? "text-status-error"
            : tone === "ok"
              ? "text-status-success"
              : "text-content-primary"
        }`}
      >
        {value}
      </div>
      {sub && <div className="text-meta text-content-muted">{sub}</div>}
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
  if (files.length === 0)
    return <div className="p-4 text-body text-content-muted">No file activity recorded.</div>;
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
  if (commands.length === 0)
    return <div className="p-4 text-body text-content-muted">No commands recorded.</div>;
  return (
    <div className="p-2">
      <table className="w-full text-left text-body">
        <thead>
          <tr className="border-b border-edge text-[9px] uppercase tracking-wider text-content-muted">
            <th className="px-2 py-1 font-medium">Tool</th>
            <th className="px-2 py-1 text-right font-medium">Calls</th>
            <th className="px-2 py-1 text-right font-medium">Failed</th>
            <th className="px-2 py-1 text-right font-medium">Output</th>
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
  if (failed.length === 0)
    return (
      <div className="p-4 text-body text-status-success">No errors. All tool calls succeeded.</div>
    );
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

function FilterChip({
  label,
  count,
  active,
  tone,
  onToggle,
}: {
  label: string;
  count: number;
  active: boolean;
  tone: "blue" | "amber" | "green" | "neutral";
  onToggle: () => void;
}) {
  const toneColors = {
    blue: active
      ? "border-status-running/40 bg-status-running-bg text-status-running"
      : "border-edge text-content-muted",
    amber: active
      ? "border-status-warning/40 bg-status-warning-bg text-status-warning"
      : "border-edge text-content-muted",
    green: active
      ? "border-status-success/40 bg-status-success-bg text-status-success"
      : "border-edge text-content-muted",
    neutral: active
      ? "border-edge-strong bg-surface-overlay text-content-secondary"
      : "border-edge text-content-muted",
  }[tone];
  return (
    <button
      type="button"
      onClick={onToggle}
      className={`rounded border px-1.5 py-0 text-[9px] font-medium uppercase tracking-wide transition-colors hover:brightness-110 ${toneColors}`}
    >
      {label} {count}
    </button>
  );
}

interface MessageFeedProps {
  messages: RunMessage[];
  filters: Filters;
  expandedTools: Set<number>;
  onToggleTool: (idx: number) => void;
  stepKey?: string;
}

function MessageFeed({
  messages,
  filters,
  expandedTools,
  onToggleTool,
  stepKey = "",
}: MessageFeedProps) {
  // Precompute per-message assistant ordinals before JSX to avoid mutation during render
  const assistantOrdinals: number[] = new Array(messages.length);
  let ordinalCount = 0;
  for (let i = 0; i < messages.length; i++) {
    if (messages[i].role === "assistant") ordinalCount += 1;
    assistantOrdinals[i] = ordinalCount;
  }

  return (
    <div className="flex flex-col">
      {messages.map((m, i) => {
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
  return (
    <details className="border-b border-edge">
      <summary className="cursor-pointer px-4 py-1.5 text-[10px] text-content-muted hover:bg-surface-overlay hover:text-content-secondary">
        <span className="font-mono uppercase tracking-wide">system</span>{" "}
        <span className="text-content-muted">{content.length.toLocaleString()} chars</span>
      </summary>
      <div className="bg-surface-base px-4 py-2">
        <p className="max-h-48 overflow-y-auto whitespace-pre-wrap font-mono text-[10px] leading-relaxed text-content-secondary">
          {content}
        </p>
      </div>
    </details>
  );
}

function UserBlock({ content, timestamp }: { content: string; timestamp?: number | null }) {
  const [open, setOpen] = useState(content.length < 200);
  return (
    <div className="border-b border-edge border-l-2 border-l-status-success bg-surface-overlay/40 px-3 py-1.5">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-2 text-left"
      >
        <span className="shrink-0 text-[9px] font-medium uppercase tracking-wide text-status-success">
          user
        </span>
        {!open && (
          <span className="min-w-0 truncate text-body text-content-secondary">
            {previewText(content, 200)}
          </span>
        )}
        {timestamp && open && (
          <span className="ml-auto shrink-0 text-[9px] text-content-muted">
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
}: {
  anchorId: string;
  content: string;
  timestamp?: number | null;
  ordinal: number;
}) {
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
          className={`shrink-0 rounded px-1 font-mono text-[9px] ${isThinking ? "bg-surface-overlay text-content-muted" : "bg-status-running-bg border border-status-running/30 text-status-running"}`}
        >
          #{ordinal}
        </span>
        <span
          className={`text-[9px] font-medium uppercase tracking-wide ${
            isThinking ? "text-content-muted" : "text-status-running"
          }`}
        >
          {isThinking ? "thinking" : "response"}
        </span>
        {timestamp && (
          <span className="ml-auto text-[9px] text-content-muted">{formatTime(timestamp)}</span>
        )}
      </div>
      <p
        className={`whitespace-pre-wrap break-words text-body leading-snug ${
          isThinking ? "text-content-muted italic" : "text-content-primary"
        }`}
      >
        {displayText}
      </p>
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
  const fn = message.function || "tool";
  const summary = message.summary || "";
  const output = message.output || "";
  const status = message.status || "ok";
  const exitCode = message.exit_code;
  const isError = status === "error";

  const statusBadge = isError ? (
    <span className="rounded border border-status-error/30 bg-status-error-bg px-1.5 py-0.5 text-[9px] font-medium text-status-error">
      {exitCode != null ? `exit ${exitCode}` : "ERR"}
    </span>
  ) : (
    <span className="rounded border border-status-success/30 bg-status-success-bg px-1.5 py-0.5 text-[9px] font-medium text-status-success">
      ✓
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
        <span className="w-4 text-center font-mono text-body text-status-warning">
          {toolIcon(fn)}
        </span>
        <span className="rounded border border-status-warning/30 bg-status-warning-bg px-1.5 py-0.5 font-mono text-meta text-status-warning">
          {fn}
        </span>
        <span
          className="flex-1 truncate font-mono text-body text-content-secondary"
          title={summary}
        >
          {summary || "(no args)"}
        </span>
        {statusBadge}
        {output && (
          <span className="text-meta text-content-muted">{formatBytes(output.length)}</span>
        )}
        <span className="text-[10px] text-content-muted">{expanded ? "▾" : "▸"}</span>
      </button>

      {expanded && (
        <div className="border-t border-edge bg-surface-raised px-4 py-2.5">
          {message.arguments && Object.keys(message.arguments).length > 1 && (
            <div className="mb-3">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-content-muted">
                arguments
              </div>
              <pre className="overflow-x-auto rounded bg-surface-overlay p-2 font-mono text-[10px] leading-relaxed text-content-secondary">
                {JSON.stringify(message.arguments, null, 2)}
              </pre>
            </div>
          )}
          <div>
            <div className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wide text-content-muted">
              <span>output</span>
              <span className="text-content-muted">{formatBytes(output.length)}</span>
            </div>
            <pre
              className={`max-h-96 overflow-auto rounded p-2 font-mono text-meta leading-relaxed ${
                isError
                  ? "bg-status-error-bg text-status-error border border-status-error/20"
                  : "bg-surface-overlay text-content-secondary"
              }`}
            >
              {output || "(no output)"}
            </pre>
          </div>
        </div>
      )}

      {!expanded && output && (
        <p className="ml-6 mr-4 pb-1 text-[10px] text-content-muted">{summarizeOutput(output)}</p>
      )}
    </div>
  );
}
