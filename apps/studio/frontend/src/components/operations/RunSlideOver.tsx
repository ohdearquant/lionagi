import { useEffect, useState } from "react";
import Button from "@/components/Button";
import Timestamp from "@/components/Timestamp";
import Duration from "@/components/Duration";
import { getSession, streamSession } from "@/lib/api";
import type { SessionDetail } from "@/lib/api";
import type { Run } from "@/lib/run-model";

type Tab = "overview" | "output";

// Phase A: Overview + Output only (ADR-0093 §Delivery A). Deeper chain
// cards, SSE live-tail rendering, and richer message adapters land phase B —
// this reads the same session data the retired detail routes use, just
// through a plain scroll of message text rather than the full step-card view.
export default function RunSlideOver({ run, onClose }: { run: Run; onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("overview");

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/30" onClick={onClose} aria-hidden="true" />
      <aside
        role="dialog"
        aria-label={run.name}
        className="fixed inset-y-0 right-0 z-50 flex w-full max-w-xl flex-col border-l border-edge bg-surface-raised"
      >
        <div className="flex items-center justify-between border-b border-edge px-4 py-3">
          <div className="min-w-0">
            <div className="truncate text-label font-semibold text-content-primary">{run.name}</div>
            <div className="text-meta text-content-muted">
              {run.source} · {run.rawStatus}
            </div>
          </div>
          <Button size="sm" variant="ghost" onClick={onClose} aria-label="Close">
            ✕
          </Button>
        </div>

        <div className="flex border-b border-edge">
          <TabButton active={tab === "overview"} onClick={() => setTab("overview")}>
            Overview
          </TabButton>
          <TabButton active={tab === "output"} onClick={() => setTab("output")}>
            Output
          </TabButton>
        </div>

        <div className="flex-1 overflow-y-auto">
          {tab === "overview" ? <OverviewTab run={run} /> : <OutputTab run={run} />}
        </div>
      </aside>
    </>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "border-b-2 px-4 py-2 text-body transition-colors",
        active
          ? "border-accent text-content-primary"
          : "border-transparent text-content-muted hover:text-content-primary",
      ].join(" ")}
    >
      {children}
    </button>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-edge-hairline px-4 py-2">
      <span className="text-meta text-content-muted">{label}</span>
      <span className="font-data text-body text-content-primary">{children}</span>
    </div>
  );
}

function OverviewTab({ run }: { run: Run }) {
  return (
    <div>
      <Field label="Source">{run.source}</Field>
      <Field label="Status">{run.status}</Field>
      <Field label="Raw status">{run.rawStatus}</Field>
      <Field label="Project">{run.project ?? "—"}</Field>
      <Field label="Started">
        <Timestamp value={run.startedAt} />
      </Field>
      <Field label="Ended">{run.endedAt != null ? <Timestamp value={run.endedAt} /> : "—"}</Field>
      <Field label="Duration">
        <Duration value={run.durationSeconds} fallback="—" />
      </Field>
      {run.reason?.exit_code != null && <Field label="Exit code">{run.reason.exit_code}</Field>}
      {run.reason?.error_detail && (
        <div className="border-b border-edge-hairline px-4 py-2">
          <div className="mb-1 text-meta text-content-muted">Error detail</div>
          <pre className="whitespace-pre-wrap break-words rounded border border-edge bg-surface-overlay p-2 font-data text-meta text-status-failure">
            {run.reason.error_detail}
          </pre>
        </div>
      )}
      {run.refs.schedule_id && <Field label="Schedule">{run.refs.schedule_id}</Field>}
      {run.refs.topic && <Field label="Topic">{run.refs.topic}</Field>}
    </div>
  );
}

function OutputTab({ run }: { run: Run }) {
  const sessionId = run.refs.session_id;
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    let active = true;
    getSession(sessionId)
      .then((s) => active && setSession(s))
      .catch((err) => active && setError(err instanceof Error ? err.message : String(err)));
    const unsubscribe =
      run.status === "running"
        ? streamSession(sessionId, () => {
            getSession(sessionId)
              .then((s) => active && setSession(s))
              .catch(() => {});
          })
        : null;
    return () => {
      active = false;
      unsubscribe?.();
    };
  }, [sessionId, run.status]);

  if (!sessionId) {
    return (
      <div className="p-4 text-body text-content-muted">
        This run has no session output to show.
      </div>
    );
  }
  if (error) {
    return <div className="p-4 text-body text-status-failure">{error}</div>;
  }
  if (!session) {
    return <div className="p-4 text-body text-content-muted">Loading…</div>;
  }

  return (
    <div className="flex flex-col gap-3 p-4">
      {session.branches.map((branch) => (
        <div key={branch.id} className="rounded border border-edge bg-surface-overlay">
          <div className="border-b border-edge px-3 py-1.5 text-meta text-content-muted">
            {branch.name}
          </div>
          <div className="flex flex-col gap-2 p-3">
            {branch.messages.length === 0 ? (
              <div className="text-meta text-content-muted">No messages.</div>
            ) : (
              branch.messages.map((m) => {
                const content = m.content ?? {};
                const text = String(
                  content.assistant_response ??
                    content.response ??
                    content.instruction ??
                    content.output ??
                    content.text ??
                    JSON.stringify(content),
                );
                return (
                  <div key={m.id} className="text-body">
                    <div className="mb-0.5 text-meta uppercase text-content-muted">{m.role}</div>
                    <pre className="whitespace-pre-wrap break-words font-data text-meta text-content-secondary">
                      {text}
                    </pre>
                  </div>
                );
              })
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
