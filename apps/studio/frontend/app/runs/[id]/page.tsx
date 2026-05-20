"use client";

import Link from "next/link";
import { use, useEffect, useRef, useState } from "react";
import Badge from "@/components/Badge";
import { getSession, streamSession } from "@/lib/api";
import type { SessionDetail, SessionBranch, SessionMessage } from "@/lib/api";

function formatRelative(sessionStart: number, ts: number): string {
  const diff = ts - sessionStart;
  if (diff < 0) return "+0.0s";
  return `+${diff.toFixed(1)}s`;
}

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString();
}

function classifyMessage(msg: SessionMessage): string {
  const lc = msg.lion_class ?? "";
  if (lc.includes("ActionRequest")) return "action_request";
  if (lc.includes("ActionResponse")) return "action_response";
  if (lc.includes("System")) return "system";
  if (lc.includes("Instruction")) return "user";
  if (lc.includes("AssistantResponse")) return "assistant";
  return msg.role;
}

function extractText(content: Record<string, unknown>, kind: string): string {
  if (kind === "system") {
    return String(content.system_message ?? content.system ?? content.guidance ?? "");
  }
  if (kind === "user") {
    return String(content.instruction ?? content.text ?? "");
  }
  if (kind === "assistant") {
    return String(content.assistant_response ?? content.response ?? "");
  }
  if (kind === "action_request") {
    const fn = String(content.function ?? content.action ?? "tool");
    const args = content.arguments ?? content.args ?? {};
    const summary = Object.entries(args as Record<string, unknown>)
      .slice(0, 3)
      .map(([k, v]) => {
        const s = String(v ?? "");
        return `${k}=${s.length > 50 ? s.slice(0, 50) + "…" : s}`;
      })
      .join(", ");
    return `${fn}(${summary})`;
  }
  if (kind === "action_response") {
    const raw = String(content.output ?? content.result ?? "");
    return raw.length > 300 ? raw.slice(0, 300) + "…" : raw;
  }
  return JSON.stringify(content).slice(0, 200);
}

function MessageBubble({ msg, sessionStart }: { msg: SessionMessage; sessionStart: number }) {
  const kind = classifyMessage(msg);
  const content = (msg.content ?? {}) as Record<string, unknown>;
  const text = extractText(content, kind);

  const kindLabel = kind.replace("_", " ").toUpperCase();
  const senderShort = msg.sender ? msg.sender.slice(0, 8) : "";

  return (
    <div className="animate-fade-in flex flex-col gap-0.5 py-1.5">
      <div className="flex items-center gap-2 text-[10px] text-content-muted">
        <span className="font-mono tabular-nums">{formatRelative(sessionStart, msg.timestamp)}</span>
        <span className="tracking-wide">{kindLabel}</span>
        {senderShort && <span className="opacity-60">· {senderShort}</span>}
      </div>

      {kind === "system" ? (
        <div className="rounded bg-surface-overlay px-3 py-1.5 text-xs italic text-content-muted">
          {text || "(system prompt)"}
        </div>
      ) : kind === "user" ? (
        <div className="self-start rounded border border-edge bg-surface-raised px-3 py-2 text-sm text-content-secondary">
          {text}
        </div>
      ) : kind === "assistant" ? (
        <div className="whitespace-pre-wrap rounded border border-edge bg-surface-base px-3 py-2 text-sm text-content-primary">
          {text}
        </div>
      ) : kind === "action_request" ? (
        <div className="rounded border border-blue-500/20 bg-blue-500/5 px-3 py-1.5 font-mono text-xs text-blue-400">
          {text}
        </div>
      ) : kind === "action_response" ? (
        <div className="max-h-40 overflow-y-auto rounded border border-edge bg-surface-overlay px-3 py-1.5 font-mono text-xs text-content-muted">
          {text}
        </div>
      ) : (
        <div className="rounded border border-edge px-3 py-1.5 text-xs text-content-muted">
          {text}
        </div>
      )}
    </div>
  );
}

function BranchSection({ branch, sessionStart }: { branch: SessionBranch; sessionStart: number }) {
  return (
    <div className="flex flex-col">
      <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-edge bg-surface-base/90 py-1.5 backdrop-blur-sm">
        <span className="text-xs font-semibold text-content-secondary">
          {branch.name || branch.id.slice(0, 8)}
        </span>
        <span className="text-xs text-content-muted">
          {branch.messages.length} msg{branch.messages.length !== 1 ? "s" : ""}
        </span>
      </div>
      <div className="flex flex-col px-1">
        {branch.messages.map((msg) => (
          <MessageBubble key={msg.id} msg={msg} sessionStart={sessionStart} />
        ))}
      </div>
    </div>
  );
}

export default function RunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const [session, setSession] = useState<SessionDetail | null>(null);
  const [live, setLive] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!id) return;
    getSession(id)
      .then(setSession)
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

  return (
    <main className="flex h-[calc(100vh-44px)] w-full overflow-hidden">
      {/* Sidebar */}
      <aside className="flex w-56 shrink-0 flex-col gap-4 border-r border-edge bg-surface-raised p-4">
        <div>
          <Link href="/runs" className="text-xs text-content-muted hover:text-content-primary">
            ← runs
          </Link>
          <h2 className="mt-2 text-base font-semibold text-content-primary">
            {session.name || session.id.slice(0, 8)}
          </h2>
          <p className="mt-0.5 font-mono text-xs text-content-muted" title={session.id}>
            {session.id.slice(0, 8)}…
          </p>
        </div>

        <dl className="flex flex-col gap-3 text-xs">
          <div>
            <dt className="text-content-muted">Created</dt>
            <dd className="text-content-secondary">{formatTime(session.created_at)}</dd>
          </div>
          <div>
            <dt className="text-content-muted">Branches</dt>
            <dd className="text-content-secondary">{session.branches.length}</dd>
          </div>
          <div>
            <dt className="text-content-muted">Messages</dt>
            <dd className="text-content-secondary">{totalMessages}</dd>
          </div>
          <div>
            <dt className="text-content-muted">Status</dt>
            <dd className="mt-0.5">
              {done ? (
                <Badge tone="ok">completed</Badge>
              ) : live ? (
                <Badge tone="running">live</Badge>
              ) : (
                <Badge tone="default">idle</Badge>
              )}
            </dd>
          </div>
        </dl>
      </aside>

      {/* Message feed */}
      <div className="flex flex-1 flex-col overflow-hidden">
        <div className="flex items-center gap-2 border-b border-edge px-4 py-2">
          <span className="text-sm font-medium text-content-primary">Messages</span>
          {live && !done && (
            <span className="flex items-center gap-1.5 text-xs text-status-running">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-status-running" />
              live
            </span>
          )}
          {done && <span className="text-xs text-content-muted">completed</span>}
          <span className="ml-auto font-mono text-xs text-content-muted">{totalMessages}</span>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          {session.branches.length === 0 ? (
            <p className="py-10 text-center text-sm text-content-muted">Waiting for messages…</p>
          ) : (
            <div className="flex flex-col gap-4">
              {session.branches.map((branch) => (
                <BranchSection key={branch.id} branch={branch} sessionStart={session.created_at} />
              ))}
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>
    </main>
  );
}
