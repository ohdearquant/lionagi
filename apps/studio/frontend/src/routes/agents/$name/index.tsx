import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import Button from "@/components/Button";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import { getAgent } from "@/lib/api";
import type { AgentProfile } from "@/lib/types";

export const Route = createFileRoute("/agents/$name/")({
  component: AgentDetailPage,
});

function messageFromError(error: unknown): string {
  return error instanceof Error ? error.message : "Failed to load agent";
}

function CodeBlock({ value }: { value: string | null }) {
  if (!value) {
    return <span className="text-content-muted">—</span>;
  }
  return (
    <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded border border-edge bg-surface-base p-3 font-mono text-body text-content-secondary">
      {value}
    </pre>
  );
}

function CopyChip({ text, label }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        try {
          void navigator.clipboard.writeText(text).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          });
        } catch {
          /* no clipboard */
        }
      }}
      className="rounded border border-edge bg-surface-raised px-2 py-0.5 font-mono text-meta text-content-muted hover:border-edge-strong hover:text-content-primary"
    >
      {copied ? "copied" : (label ?? "copy")}
    </button>
  );
}

function AgentDetailPage() {
  const { name } = Route.useParams();
  const agentName = name;
  const [agent, setAgent] = useState<AgentProfile | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    async function loadAgent() {
      try {
        const data = await getAgent(agentName);
        if (active) {
          setAgent(data);
          setError(null);
        }
      } catch (err) {
        if (active) {
          setError(messageFromError(err));
        }
      }
    }

    void loadAgent();

    return () => {
      active = false;
    };
  }, [agentName]);

  return (
    <main className="mx-auto flex w-full max-w-4xl flex-col gap-4 px-4 py-6 text-content-primary">
      <PageHeader
        density="loose"
        breadcrumb={[
          <Link key="agents" to="/agents" className="hover:text-content-primary">
            agents
          </Link>,
          <span key="name" className="text-content-secondary">
            {agent?.name ?? agentName}
          </span>,
        ]}
        title={agent?.name ?? agentName}
        badges={
          agent ? (
            <div className="flex flex-wrap items-center gap-2">
              {agent.provider ? (
                <StatusPill value={agent.provider} kind="role" tone="neutral" />
              ) : null}
              {agent.model ? (
                <span className="rounded-full border border-edge bg-surface-overlay px-2 py-0.5 font-mono text-meta text-content-secondary">
                  {agent.model}
                </span>
              ) : null}
              {agent.permission_mode ? (
                <StatusPill
                  value={`perm: ${agent.permission_mode}`}
                  kind="neutral"
                  tone="pending"
                />
              ) : null}
              {agent.reasoning_effort ? (
                <StatusPill
                  value={`effort: ${agent.reasoning_effort}`}
                  kind="neutral"
                  tone="pending"
                />
              ) : null}
            </div>
          ) : null
        }
        subtitle={agent?.description ?? undefined}
        actions={
          <Link to="/agents/$name/edit" params={{ name: agentName }}>
            <Button variant="primary" size="sm" leading="✎">
              Edit
            </Button>
          </Link>
        }
      />

      {error ? (
        <div className="rounded border border-status-failure/30 bg-status-failure/10 px-3 py-2 text-body text-status-failure">
          {error}
        </div>
      ) : null}

      {!agent && !error ? (
        <div className="py-10 text-center text-body text-content-muted">Loading...</div>
      ) : null}

      {agent ? (
        <div className="flex flex-col gap-4">
          <section className="rounded border border-edge bg-surface-overlay px-4 py-3">
            <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 text-meta">
              <SummaryChip label="Provider" value={agent.provider || "—"} mono />
              <SummaryChip label="Model" value={agent.model || "—"} mono />
              <SummaryChip label="Permission" value={agent.permission_mode || "default"} mono />
              <SummaryChip label="Effort" value={agent.reasoning_effort || "none"} mono />
              {agent.path ? (
                <SummaryChip label="Source" value={agent.path} mono className="max-w-[28rem]" />
              ) : null}
            </div>
          </section>

          <section className="flex flex-col gap-3 rounded border border-edge bg-surface-raised p-4">
            <div className="flex items-center justify-between">
              <h2 className="text-label font-semibold text-content-primary">System Prompt</h2>
              {agent.system_prompt ? <CopyChip text={agent.system_prompt} /> : null}
            </div>
            <CodeBlock value={agent.system_prompt} />
          </section>

          <section className="flex flex-col gap-3 rounded border border-edge bg-surface-raised p-4">
            <div className="flex items-center justify-between">
              <h2 className="text-label font-semibold text-content-primary">Guidance</h2>
              {agent.guidance ? <CopyChip text={agent.guidance} /> : null}
            </div>
            <CodeBlock value={agent.guidance} />
          </section>
        </div>
      ) : null}
    </main>
  );
}

function SummaryChip({
  label,
  value,
  mono,
  className,
}: {
  label: string;
  value: string;
  mono?: boolean;
  className?: string;
}) {
  return (
    <div className={["flex min-w-0 items-center gap-1.5", className].filter(Boolean).join(" ")}>
      <span className="uppercase tracking-[0.06em] text-content-muted">{label}</span>
      <span
        className={["truncate text-content-primary", mono ? "font-mono" : ""].join(" ")}
        title={value}
      >
        {value}
      </span>
    </div>
  );
}
