"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { use, useEffect, useMemo, useRef, useState } from "react";
import Badge from "@/components/Badge";
import RunStepCard from "@/components/RunStepCard";
import { getSession, streamSession } from "@/lib/api";
import type { SessionDetail, SessionBranch, SessionMessage } from "@/lib/api";
import type { RunMessage, RunStep, WorkerGraph } from "@/lib/types";

const WorkerCanvas = dynamic(() => import("@/components/canvas/WorkerCanvas"), { ssr: false });

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString();
}

function formatDuration(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

// ── Adapter: SessionBranch → RunStep with RunMessages ────────────────────────

function classifyLC(lc: string): string {
  if (lc.includes("ActionRequest")) return "action_request";
  if (lc.includes("ActionResponse")) return "action_response";
  if (lc.includes("System")) return "system";
  if (lc.includes("Instruction")) return "user";
  if (lc.includes("AssistantResponse")) return "assistant";
  return "unknown";
}

function branchToRunStep(branch: SessionBranch, status: string): RunStep {
  const msgs = branch.messages;
  const runMessages: RunMessage[] = [];

  const responseById = new Map<string, SessionMessage>();
  for (const m of msgs) {
    if (classifyLC(m.lion_class) === "action_response") {
      responseById.set(m.id, m);
    }
  }
  const pairedResponseIds = new Set<string>();

  for (const m of msgs) {
    const kind = classifyLC(m.lion_class);
    const content = (m.content ?? {}) as Record<string, unknown>;

    if (kind === "system") {
      const text = String(content.system_message ?? content.system ?? content.guidance ?? "");
      if (text)
        runMessages.push({
          role: "system",
          content: text,
          sender: m.sender ?? "",
          timestamp: m.timestamp,
        });
      continue;
    }

    if (kind === "user") {
      runMessages.push({
        role: "user",
        content: String(content.instruction ?? content.text ?? JSON.stringify(content)),
        sender: m.sender ?? "",
        timestamp: m.timestamp,
      });
      continue;
    }

    if (kind === "assistant") {
      runMessages.push({
        role: "assistant",
        content: String(content.assistant_response ?? content.response ?? ""),
        sender: m.sender ?? "",
        timestamp: m.timestamp,
      });
      continue;
    }

    if (kind === "action_request") {
      const fn = String(content.function ?? "");
      const args = (content.arguments ?? {}) as Record<string, unknown>;
      const respId = content.action_response_id ? String(content.action_response_id) : null;
      const respMsg = respId ? responseById.get(respId) : null;
      if (respMsg) pairedResponseIds.add(respMsg.id);

      const respContent = respMsg ? ((respMsg.content ?? {}) as Record<string, unknown>) : {};
      const output = respMsg ? String(respContent.output ?? "") : "";

      const summary = Object.entries(args)
        .slice(0, 2)
        .map(([k, v]) => {
          const s = String(v ?? "");
          return s.length > 60 ? `${k}=${s.slice(0, 60)}…` : `${k}=${s}`;
        })
        .join(", ");

      runMessages.push({
        role: "tool_call",
        function: fn,
        summary,
        arguments: args,
        output,
        status: output.toLowerCase().includes("error") ? "error" : "ok",
        sender: m.sender ?? "",
        timestamp: m.timestamp,
      });
      continue;
    }

    if (kind === "action_response" && !pairedResponseIds.has(m.id)) {
      const fn = String(content.function ?? "");
      const output = String(content.output ?? "");
      runMessages.push({
        role: "tool_call",
        function: fn,
        output,
        status: "ok",
        sender: m.sender ?? "",
        timestamp: m.timestamp,
      });
    }
  }

  const rolesCounts: Record<string, number> = {};
  for (const rm of runMessages) {
    rolesCounts[rm.role] = (rolesCounts[rm.role] ?? 0) + 1;
  }

  return {
    step: branch.name || branch.id.slice(0, 8),
    status,
    result: {
      agent: branch.agent_name ?? branch.name ?? branch.id.slice(0, 8),
      model: branch.model ?? branch.provider ?? null,
      message_count: runMessages.length,
      roles: rolesCounts,
    },
    messages: runMessages,
    timestamp: branch.created_at,
  };
}

// ── Section Nav ───────────────────────────────────────────────────────────────

interface NavSection {
  id: string;
  label: string;
  count?: number;
  errorTone?: boolean;
}

function SectionNav({ sections, activeId }: { sections: NavSection[]; activeId: string }) {
  const scrollTo = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
  };

  return (
    <nav className="sticky top-[calc(2.75rem+2.5rem)] z-20 flex items-center gap-1 border-b border-edge bg-surface-base px-3 py-1.5 xl:px-4">
      {sections.map((s) => {
        const isActive = s.id === activeId;
        return (
          <button
            key={s.id}
            type="button"
            onClick={() => scrollTo(s.id)}
            className={`flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors ${
              isActive
                ? "bg-interactive-secondary text-content-primary"
                : "text-content-muted hover:bg-surface-overlay hover:text-content-secondary"
            }`}
          >
            {s.label}
            {s.count != null && (
              <span
                className={`rounded px-1 font-mono text-[9px] ${
                  s.errorTone && s.count > 0
                    ? "bg-status-error-bg text-status-error"
                    : "bg-surface-overlay text-content-muted"
                }`}
              >
                {s.count}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}

// ── Overview section ─────────────────────────────────────────────────────────

interface OverviewData {
  status: string;
  durationSec: number | null;
  branchCount: number;
  messageCount: number;
  toolCallCount: number;
  errorCount: number;
  showTopic?: string | null;
  showPlayName?: string | null;
  playbookName?: string | null;
}

function OverviewSection({ data }: { data: OverviewData }) {
  const stats: Array<{ label: string; value: string; tone?: "ok" | "error" }> = [
    { label: "Status", value: data.status },
    ...(data.durationSec != null
      ? [{ label: "Duration", value: formatDuration(data.durationSec) }]
      : []),
    { label: "Branches", value: String(data.branchCount) },
    { label: "Messages", value: String(data.messageCount) },
    { label: "Tool calls", value: String(data.toolCallCount) },
    {
      label: "Errors",
      value: String(data.errorCount),
      tone: data.errorCount > 0 ? ("error" as const) : ("ok" as const),
    },
  ];

  const provenance = [
    data.showTopic && { label: "Topic", value: data.showTopic },
    data.showPlayName && { label: "Play", value: data.showPlayName },
    data.playbookName && { label: "Playbook", value: data.playbookName },
  ].filter(Boolean) as Array<{ label: string; value: string }>;

  return (
    <div id="overview" className="scroll-mt-24">
      <SectionHeader label="Overview" />
      <div className="rounded border border-edge bg-surface-raised px-4 py-3 shadow-card">
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
          {stats.map((s) => (
            <div key={s.label} className="flex flex-col gap-0.5">
              <span className="text-[9px] font-semibold uppercase tracking-wider text-content-muted">
                {s.label}
              </span>
              <span
                className={`font-mono text-label font-semibold tabular-nums tracking-tight ${
                  s.tone === "error"
                    ? "text-status-error"
                    : s.tone === "ok"
                      ? "text-status-success"
                      : "text-content-primary"
                }`}
              >
                {s.value}
              </span>
            </div>
          ))}
        </div>
        {provenance.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-3 border-t border-edge-subtle pt-3">
            {provenance.map((p) => (
              <div key={p.label} className="flex items-center gap-1.5">
                <span className="text-[9px] uppercase tracking-wide text-content-muted">
                  {p.label}
                </span>
                <span className="font-mono text-meta text-content-secondary">{p.value}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Branches section ─────────────────────────────────────────────────────────

function BranchesSection({
  steps,
  live,
  expandedSteps,
  onToggleExpand,
}: {
  steps: RunStep[];
  live: boolean;
  expandedSteps: Set<string>;
  onToggleExpand: (stepId: string, next: boolean) => void;
}) {
  return (
    <div id="branches" className="scroll-mt-24">
      <SectionHeader label="Branches" count={steps.length} />
      <div className="flex flex-col gap-1.5">
        {steps.length === 0 ? (
          <div className="border border-edge bg-surface-base px-3 py-10 text-center text-sm text-content-muted">
            {live ? (
              <span className="flex items-center justify-center gap-2">
                <span className="relative flex h-2 w-2">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-status-running opacity-75" />
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-status-running" />
                </span>
                Waiting for messages…
              </span>
            ) : (
              "No messages recorded"
            )}
          </div>
        ) : (
          steps.map((step) => (
            <RunStepCard
              key={step.step}
              step={step}
              expanded={expandedSteps.has(step.step)}
              onToggleExpand={onToggleExpand}
            />
          ))
        )}
      </div>
    </div>
  );
}

// ── Errors section ───────────────────────────────────────────────────────────

interface ErrorEntry {
  fn: string;
  branch: string;
  timestamp: number | null;
  output: string;
  summary?: string;
}

function ErrorsSection({ errors }: { errors: ErrorEntry[] }) {
  // Group by function name
  const groups = useMemo(() => {
    const map = new Map<string, ErrorEntry[]>();
    for (const err of errors) {
      const list = map.get(err.fn) ?? [];
      list.push(err);
      map.set(err.fn, list);
    }
    return Array.from(map.entries()).sort((a, b) => b[1].length - a[1].length);
  }, [errors]);

  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());

  const toggleGroup = (fn: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(fn)) next.delete(fn);
      else next.add(fn);
      return next;
    });
  };

  return (
    <div id="errors" className="scroll-mt-24">
      <SectionHeader label="Errors" count={errors.length} errorTone={errors.length > 0} />
      {errors.length === 0 ? (
        <div className="flex items-center gap-2 rounded border border-edge bg-surface-raised px-4 py-3 text-sm text-status-success">
          <span>No errors detected across all branches.</span>
        </div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {groups.map(([fn, errs]) => {
            const isOpen = expandedGroups.has(fn);
            const first = errs[0];
            return (
              <div
                key={fn}
                className="rounded border border-l-2 border-edge border-l-status-error bg-surface-raised"
              >
                <button
                  type="button"
                  aria-expanded={isOpen}
                  onClick={() => toggleGroup(fn)}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface-overlay"
                >
                  <span className="mt-0.5 text-body text-content-muted">{isOpen ? "▾" : "▸"}</span>
                  <span className="font-mono text-[11px] font-semibold text-status-error">
                    {fn}
                  </span>
                  <span className="rounded bg-status-error-bg px-1.5 py-0 font-mono text-[9px] text-status-error">
                    ×{errs.length}
                  </span>
                  <span className="text-[10px] text-content-muted">
                    first in{" "}
                    <span className="font-mono text-content-secondary">{first.branch}</span>
                    {first.timestamp != null && (
                      <>
                        {" "}
                        ·{" "}
                        {new Date(first.timestamp * 1000).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                          second: "2-digit",
                        })}
                      </>
                    )}
                  </span>
                  {!isOpen && first.output && (
                    <span className="ml-auto truncate max-w-xs font-mono text-[10px] text-content-muted">
                      {first.output.split("\n")[0]?.slice(0, 80)}
                    </span>
                  )}
                </button>
                {isOpen && (
                  <div className="flex flex-col gap-2 border-t border-edge px-3 pb-2 pt-2">
                    {errs.map((err, i) => (
                      <div key={i} className="flex flex-col gap-1">
                        <div className="flex items-center gap-2 text-[10px]">
                          <span className="font-mono text-content-secondary">{err.branch}</span>
                          {err.timestamp != null && (
                            <span className="text-content-muted">
                              {new Date(err.timestamp * 1000).toLocaleTimeString([], {
                                hour: "2-digit",
                                minute: "2-digit",
                                second: "2-digit",
                              })}
                            </span>
                          )}
                        </div>
                        {err.summary && (
                          <p className="truncate font-mono text-[10px] text-content-secondary">
                            $ {err.summary}
                          </p>
                        )}
                        {err.output && (
                          <pre className="max-h-32 overflow-auto rounded border border-status-error/20 bg-status-error-bg p-2 font-mono text-[10px] leading-relaxed text-status-error">
                            {err.output.length > 1500
                              ? err.output.slice(0, 1500) + "\n…[truncated]"
                              : err.output}
                          </pre>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Files section ─────────────────────────────────────────────────────────────

function FilesSection({ files }: { files: string[] }) {
  return (
    <div id="files" className="scroll-mt-24">
      <SectionHeader label="Files" count={files.length} />
      {files.length === 0 ? (
        <div className="rounded border border-edge bg-surface-raised px-4 py-3 text-sm text-content-muted">
          No file operations detected.
        </div>
      ) : (
        <div className="rounded border border-edge bg-surface-raised px-3 py-2">
          <ul className="flex flex-col gap-0.5">
            {files.map((f) => (
              <li key={f} className="font-mono text-[11px] text-content-secondary">
                {f}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ── Shared section header ─────────────────────────────────────────────────────

function SectionHeader({
  label,
  count,
  errorTone,
}: {
  label: string;
  count?: number;
  errorTone?: boolean;
}) {
  return (
    <div className="mb-2 flex items-center gap-2">
      <h2 className="text-label font-semibold text-content-primary">{label}</h2>
      {count != null && (
        <span
          className={`rounded px-1.5 py-0 font-mono text-[9px] ${
            errorTone && count > 0
              ? "bg-status-error-bg text-status-error"
              : "bg-surface-overlay text-content-muted"
          }`}
        >
          {count}
        </span>
      )}
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function RunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const [session, setSession] = useState<SessionDetail | null>(null);
  const [runGraph, setRunGraph] = useState<WorkerGraph | null>(null);
  const [live, setLive] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const [expandedSteps, setExpandedSteps] = useState<Set<string>>(new Set());
  const [activeSection, setActiveSection] = useState("overview");

  useEffect(() => {
    if (!id) return;
    getSession(id)
      .then((s) => {
        setSession(s);
        if (s.branches.length <= 3) {
          setExpandedSteps(new Set(s.branches.map((b) => b.name || b.id.slice(0, 8))));
        } else {
          const first = s.branches[0];
          if (first) {
            setExpandedSteps(new Set([first.name || first.id.slice(0, 8)]));
          }
        }
        const graph = (s as unknown as Record<string, unknown>).graph as
          | { nodes: WorkerGraph["nodes"]; edges: WorkerGraph["edges"] }
          | null
          | undefined;
        if (graph && graph.nodes && graph.nodes.length > 0) {
          setRunGraph({ name: s.name || id, description: "", nodes: graph.nodes, edges: graph.edges });
        }
      })
      .catch((e: unknown) => setError(String(e)));
  }, [id]);

  useEffect(() => {
    if (!id) return;

    const stop = streamSession(id, (event) => {
      if (event.type === "heartbeat") return;

      if (event.type === "done") {
        setDone(true);
        setLive(false);
        return;
      }

      setLive(true);

      if (event.id && event.role && event.branch_id) {
        const msg = event as unknown as SessionMessage;
        setSession((prev) => {
          if (!prev) return prev;
          const branchId = String(event.branch_id);
          const existing = prev.branches.find((b) => b.id === branchId);

          if (existing) {
            if (existing.messages.some((m) => m.id === msg.id)) return prev;
            return {
              ...prev,
              branches: prev.branches.map((b) =>
                b.id === branchId ? { ...b, messages: [...b.messages, msg] } : b,
              ),
            };
          }

          return {
            ...prev,
            branches: [
              ...prev.branches,
              {
                id: branchId,
                name: branchId.slice(0, 8),
                created_at: msg.timestamp,
                messages: [msg],
              },
            ],
          };
        });
      }
    });

    return stop;
  }, [id]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [session?.branches]);

  // Track active section on scroll
  useEffect(() => {
    const sectionIds = ["overview", "dag", "branches", "errors", "files"];
    const onScroll = () => {
      for (const sid of [...sectionIds].reverse()) {
        const el = document.getElementById(sid);
        if (el && el.getBoundingClientRect().top <= 160) {
          setActiveSection(sid);
          return;
        }
      }
      setActiveSection("overview");
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const handleToggleExpand = (stepId: string, next: boolean) => {
    setExpandedSteps((prev) => {
      const updated = new Set(prev);
      if (next) updated.add(stepId);
      else updated.delete(stepId);
      return updated;
    });
  };

  const sessionStatus = done ? "completed" : live ? "running" : "completed";

  // Segments from session metadata — maps op executions to branch time windows
  const segments = useMemo(() => {
    if (!session) return [] as Array<{ op_id: string; branch_id: string; branch_name: string; status: string; started_at: number | null; ended_at: number | null }>;
    const raw = (session as unknown as Record<string, unknown>).segments;
    return (Array.isArray(raw) ? raw : []) as Array<{ op_id: string; branch_id: string; branch_name: string; status: string; started_at: number | null; ended_at: number | null }>;
  }, [session]);

  const steps = useMemo(() => {
    if (!session) return [];
    const result: RunStep[] = [];

    for (const b of session.branches) {
      const bStatus = (b as unknown as Record<string, unknown>).status as string | null;
      const branchSegs = segments.filter((s) => s.branch_id === b.id);

      if (branchSegs.length <= 1) {
        result.push(branchToRunStep(b, bStatus || sessionStatus));
      } else {
        for (const seg of branchSegs) {
          const segMsgs = b.messages.filter((m) => {
            const ts = m.timestamp;
            if (ts == null) return false;
            const after = seg.started_at == null || ts >= seg.started_at;
            const before = seg.ended_at == null || ts <= seg.ended_at + 1;
            return after && before;
          });
          const segBranch = { ...b, messages: segMsgs, name: `${b.name || b.id.slice(0, 8)} [${seg.op_id}]` };
          result.push(branchToRunStep(segBranch, seg.status || bStatus || sessionStatus));
        }
      }
    }
    return result;
  }, [session, sessionStatus, segments]);

  // Extract errors across all branches
  const errors = useMemo(() => {
    const errs: ErrorEntry[] = [];
    for (const step of steps) {
      for (const msg of step.messages ?? []) {
        if (msg.role === "tool_call" && msg.status === "error") {
          errs.push({
            fn: msg.function ?? "unknown",
            branch: step.step,
            timestamp: msg.timestamp ?? null,
            output: msg.output ?? "",
            summary: msg.summary,
          });
        }
      }
    }
    return errs;
  }, [steps]);

  // Extract unique file paths across all branches
  const files = useMemo(() => {
    const paths = new Set<string>();
    for (const step of steps) {
      for (const msg of step.messages ?? []) {
        if (msg.role === "tool_call" && msg.arguments) {
          const fp = msg.arguments.file_path || msg.arguments.path;
          if (typeof fp === "string") paths.add(fp);
        }
      }
    }
    return Array.from(paths).sort();
  }, [steps]);

  if (error) {
    return (
      <main className="flex items-center justify-center py-20">
        <div className="rounded border border-status-error/30 bg-status-error-bg px-4 py-3 text-body text-status-error shadow-card">
          {error}
        </div>
      </main>
    );
  }

  if (!session) {
    return (
      <main className="flex items-center justify-center py-20">
        <div className="flex flex-col items-center gap-3">
          <div className="flex gap-1">
            <span
              className="block h-2 w-2 rounded-full bg-content-muted opacity-60 animate-bounce"
              style={{ animationDelay: "0ms" }}
            />
            <span
              className="block h-2 w-2 rounded-full bg-content-muted opacity-60 animate-bounce"
              style={{ animationDelay: "150ms" }}
            />
            <span
              className="block h-2 w-2 rounded-full bg-content-muted opacity-60 animate-bounce"
              style={{ animationDelay: "300ms" }}
            />
          </div>
          <p className="text-meta text-content-muted">Loading session…</p>
        </div>
      </main>
    );
  }

  const totalMessages = session.branches.reduce((n, b) => n + b.messages.length, 0);

  // Derive duration from session timestamps
  const durationSec =
    session.created_at != null && session.updated_at != null
      ? Math.max(0, Math.round(session.updated_at - session.created_at))
      : null;

  // Total tool call count
  const toolCallCount = steps.reduce((n, s) => {
    return n + (s.messages ?? []).filter((m) => m.role === "tool_call").length;
  }, 0);

  const overviewData: OverviewData = {
    status: done ? "completed" : live ? "running" : "idle",
    durationSec,
    branchCount: session.branches.length,
    messageCount: totalMessages,
    toolCallCount,
    errorCount: errors.length,
    // These may be null/undefined for sessions without enriched metadata
    showTopic: (session as unknown as Record<string, unknown>).show_topic as
      | string
      | null
      | undefined,
    showPlayName: (session as unknown as Record<string, unknown>).show_play_name as
      | string
      | null
      | undefined,
    playbookName: (session as unknown as Record<string, unknown>).playbook_name as
      | string
      | null
      | undefined,
  };

  const navSections: NavSection[] = [
    { id: "overview", label: "Overview" },
    ...(runGraph ? [{ id: "dag", label: "DAG", count: runGraph.nodes.length }] : []),
    { id: "branches", label: "Branches", count: session.branches.length },
    { id: "errors", label: "Errors", count: errors.length, errorTone: errors.length > 0 },
    { id: "files", label: "Files", count: files.length },
  ];

  return (
    <div className="flex min-h-screen w-full flex-col bg-surface-base text-content-primary animate-page-enter">
      {/* Header */}
      <header className="sticky top-11 z-30 flex items-center gap-3 border-b border-edge bg-surface-base px-3 py-1.5 xl:px-4">
        <Link
          href="/runs"
          className="shrink-0 text-sm text-content-secondary hover:text-content-primary"
        >
          ← runs
        </Link>
        <span className="text-content-muted">/</span>
        <h1 className="min-w-0 flex-1 truncate font-mono text-base font-semibold text-content-primary">
          {session.name || session.id.slice(0, 8)}
        </h1>
        <Badge tone={done ? "ok" : live ? "running" : "default"}>
          {done ? "completed" : live ? "running" : "idle"}
        </Badge>
        {live && !done && (
          <span className="flex shrink-0 items-center gap-1.5 text-xs text-status-success">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-status-success opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-status-success" />
            </span>
            live
          </span>
        )}
      </header>

      {/* Section nav */}
      <SectionNav sections={navSections} activeId={activeSection} />

      {/* Content */}
      <div className="flex flex-1 gap-3 px-3 py-3 xl:px-4">
        {/* Left sidebar */}
        <aside className="hidden lg:flex lg:w-56 lg:shrink-0 lg:flex-col">
          <div className="sticky top-[7.5rem] flex flex-col gap-3">
            <div className="rounded border border-edge bg-surface-raised px-3 py-2">
              <div className="mb-1.5 text-[9px] font-semibold uppercase tracking-wider text-content-muted">
                Session
              </div>
              <dl className="flex flex-col gap-1">
                <div className="flex items-start justify-between gap-2">
                  <dt className="shrink-0 text-[10px] text-content-secondary">ID</dt>
                  <dd
                    className="min-w-0 truncate text-right font-mono text-[10px] text-content-primary"
                    title={session.id}
                  >
                    {session.id.slice(0, 12)}…
                  </dd>
                </div>
                <div className="flex items-start justify-between gap-2">
                  <dt className="shrink-0 text-[10px] text-content-secondary">Started</dt>
                  <dd className="text-right text-[10px] text-content-primary">
                    {formatTime(session.created_at)}
                  </dd>
                </div>
                <div className="flex items-start justify-between gap-2">
                  <dt className="shrink-0 text-[10px] text-content-secondary">Branches</dt>
                  <dd className="text-right text-[10px] text-content-primary">
                    {session.branches.length}
                  </dd>
                </div>
                <div className="flex items-start justify-between gap-2">
                  <dt className="shrink-0 text-[10px] text-content-secondary">Messages</dt>
                  <dd className="text-right text-[10px] text-content-primary">{totalMessages}</dd>
                </div>
              </dl>
            </div>
          </div>
        </aside>

        {/* Center — anchored sections */}
        <main className="min-w-0 flex-1">
          <div className="flex flex-col gap-8">
            <OverviewSection data={overviewData} />
            {runGraph && (
              <div id="dag" className="scroll-mt-24">
                <SectionHeader label="Execution DAG" count={runGraph.nodes.length} />
                <div className="h-[320px] rounded border border-edge bg-surface-raised shadow-card overflow-hidden">
                  <WorkerCanvas
                    graph={runGraph}
                    editable={false}
                    execSteps={steps.map((s) => ({
                      step: s.step,
                      status: s.status,
                      result: s.result,
                      timestamp: s.timestamp ?? undefined,
                    }))}
                  />
                </div>
              </div>
            )}
            <BranchesSection
              steps={steps}
              live={live}
              expandedSteps={expandedSteps}
              onToggleExpand={handleToggleExpand}
            />
            <ErrorsSection errors={errors} />
            <FilesSection files={files} />
          </div>
          <div ref={bottomRef} />
        </main>
      </div>
    </div>
  );
}
