"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import Badge from "@/components/Badge";
import { getAgent } from "@/lib/api";
import type { AgentProfile } from "@/lib/types";

function messageFromError(error: unknown): string {
  return error instanceof Error ? error.message : "Failed to load agent";
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs uppercase text-neutral-500">{label}</span>
      <div className="text-sm text-neutral-300">{children}</div>
    </div>
  );
}

function CodeBlock({ value }: { value: string }) {
  if (!value) {
    return <span className="text-neutral-600">—</span>;
  }
  return (
    <pre className="whitespace-pre-wrap break-words rounded border border-neutral-800 bg-neutral-900 p-3 font-mono text-xs text-neutral-300">
      {value}
    </pre>
  );
}

export default function AgentDetailPage({ params }: { params: { name: string } }) {
  const agentName = decodeURIComponent(params.name);
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
    <main className="mx-auto flex w-full max-w-4xl flex-col gap-4 px-4 py-6 text-neutral-200">
      {/* Header */}
      <header className="flex flex-col gap-3 border-b border-neutral-800 pb-4">
        <Link href="/agents" className="text-sm text-neutral-500 hover:text-neutral-200">
          / agents
        </Link>

        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="truncate text-xl font-semibold text-neutral-200">
              {agent?.name ?? agentName}
            </h1>
            {agent?.description ? (
              <p className="mt-1 text-sm text-neutral-400">{agent.description}</p>
            ) : null}
          </div>
          <Link
            href={`/agents/${encodeURIComponent(agentName)}/edit`}
            className="shrink-0 rounded border border-neutral-700 bg-neutral-900 px-3 py-1 text-sm text-neutral-300 hover:border-neutral-500 hover:text-neutral-200"
          >
            Edit
          </Link>
        </div>
      </header>

      {error ? (
        <div className="border border-red-800 bg-neutral-950 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      ) : null}

      {!agent && !error ? (
        <div className="py-10 text-center text-sm text-neutral-500">Loading...</div>
      ) : null}

      {agent ? (
        <div className="flex flex-col gap-5">
          {/* Provider + Model badges */}
          <section className="flex flex-col gap-3 rounded border border-neutral-800 bg-neutral-950 p-4">
            <h2 className="text-sm font-semibold text-neutral-200">Model</h2>
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="default">{agent.provider}</Badge>
              <Badge tone="default">{agent.model}</Badge>
              {agent.permission_mode ? <Badge tone="pending">{agent.permission_mode}</Badge> : null}
              {agent.reasoning_effort ? (
                <Badge tone="pending">effort: {agent.reasoning_effort}</Badge>
              ) : null}
            </div>
          </section>

          {/* System Prompt */}
          <section className="flex flex-col gap-3 rounded border border-neutral-800 bg-neutral-950 p-4">
            <h2 className="text-sm font-semibold text-neutral-200">System Prompt</h2>
            <CodeBlock value={agent.system_prompt} />
          </section>

          {/* Guidance */}
          <section className="flex flex-col gap-3 rounded border border-neutral-800 bg-neutral-950 p-4">
            <h2 className="text-sm font-semibold text-neutral-200">Guidance</h2>
            <CodeBlock value={agent.guidance} />
          </section>
        </div>
      ) : null}
    </main>
  );
}
