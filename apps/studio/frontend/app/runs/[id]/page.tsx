"use client";

import Link from "next/link";
import { use, useEffect, useRef, useState } from "react";
import Badge from "@/components/Badge";
import RunStepCard from "@/components/RunStepCard";
import { getSession, streamSession } from "@/lib/api";
import type { SessionDetail, SessionBranch, SessionMessage } from "@/lib/api";
import type { RunMessage, RunStep } from "@/lib/types";

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString();
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
      if (text) runMessages.push({ role: "system", content: text, sender: m.sender ?? "", timestamp: m.timestamp });
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

      const respContent = respMsg ? (respMsg.content ?? {}) as Record<string, unknown> : {};
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
      agent: branch.name || branch.id.slice(0, 8),
      message_count: runMessages.length,
      roles: rolesCounts,
    },
    messages: runMessages,
    timestamp: branch.created_at,
  };
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function RunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const [session, setSession] = useState<SessionDetail | null>(null);
  const [live, setLive] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const [expandedSteps, setExpandedSteps] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!id) return;
    getSession(id)
      .then((s) => {
        setSession(s);
        if (s.branches.length <= 3) {
          setExpandedSteps(new Set(s.branches.map((b) => b.name || b.id.slice(0, 8))));
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
              { id: branchId, name: branchId.slice(0, 8), created_at: msg.timestamp, messages: [msg] },
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

  const handleToggleExpand = (stepId: string, next: boolean) => {
    setExpandedSteps((prev) => {
      const updated = new Set(prev);
      if (next) updated.add(stepId);
      else updated.delete(stepId);
      return updated;
    });
  };

  if (error) {
    return (
      <main className="flex items-center justify-center py-20">
        <p className="text-sm text-status-error">{error}</p>
      </main>
    );
  }

  if (!session) {
    return (
      <main className="flex items-center justify-center py-20">
        <p className="text-sm text-content-muted">Loading...</p>
      </main>
    );
  }

  const totalMessages = session.branches.reduce((n, b) => n + b.messages.length, 0);
  const branchStatus = done ? "completed" : live ? "running" : "completed";
  const steps = session.branches.map((b) => branchToRunStep(b, branchStatus));

  return (
    <div className="flex min-h-screen w-full flex-col bg-surface-base text-content-primary">
      {/* Header */}
      <header className="sticky top-11 z-30 flex items-center gap-3 border-b border-edge bg-surface-base px-3 py-1.5 xl:px-4">
        <Link href="/runs" className="shrink-0 text-sm text-content-secondary hover:text-content-primary">
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

      {/* Content */}
      <div className="flex flex-1 gap-3 px-3 py-3 xl:px-4">
        {/* Left sidebar */}
        <aside className="hidden lg:flex lg:w-56 lg:shrink-0 lg:flex-col">
          <div className="sticky top-[5.25rem] flex flex-col gap-3">
            <div className="rounded border border-edge bg-surface-raised px-3 py-2">
              <div className="mb-1.5 text-[9px] font-semibold uppercase tracking-wider text-content-muted">
                Session
              </div>
              <dl className="flex flex-col gap-1">
                <div className="flex items-start justify-between gap-2">
                  <dt className="shrink-0 text-[10px] text-content-secondary">ID</dt>
                  <dd className="min-w-0 truncate text-right font-mono text-[10px] text-content-primary" title={session.id}>
                    {session.id.slice(0, 12)}…
                  </dd>
                </div>
                <div className="flex items-start justify-between gap-2">
                  <dt className="shrink-0 text-[10px] text-content-secondary">Started</dt>
                  <dd className="text-right text-[10px] text-content-primary">{formatTime(session.created_at)}</dd>
                </div>
                <div className="flex items-start justify-between gap-2">
                  <dt className="shrink-0 text-[10px] text-content-secondary">Branches</dt>
                  <dd className="text-right text-[10px] text-content-primary">{session.branches.length}</dd>
                </div>
                <div className="flex items-start justify-between gap-2">
                  <dt className="shrink-0 text-[10px] text-content-secondary">Messages</dt>
                  <dd className="text-right text-[10px] text-content-primary">{totalMessages}</dd>
                </div>
              </dl>
            </div>
          </div>
        </aside>

        {/* Center — step cards */}
        <main className="min-w-0 flex-1">
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
                  onToggleExpand={handleToggleExpand}
                />
              ))
            )}
          </div>
          <div ref={bottomRef} />
        </main>
      </div>
    </div>
  );
}
