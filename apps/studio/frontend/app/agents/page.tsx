"use client";

import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import Table, { type TableColumn } from "@/components/Table";
import { listAgents } from "@/lib/api";
import type { AgentProfileSummary } from "@/lib/types";

function messageFromError(error: unknown): string {
  return error instanceof Error ? error.message : "Failed to load agents";
}

export default function AgentsPage() {
  const router = useRouter();
  const [agents, setAgents] = useState<AgentProfileSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    async function loadAgents() {
      try {
        const response = await listAgents();
        if (active) {
          setAgents(response.agents);
          setError(null);
        }
      } catch (err) {
        if (active) {
          setError(messageFromError(err));
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    }

    void loadAgents();

    return () => {
      active = false;
    };
  }, []);

  const columns = useMemo<Array<TableColumn<AgentProfileSummary>>>(
    () => [
      {
        id: "name",
        header: "name",
        accessor: (row) => (
          <div className="min-w-0">
            <div className="truncate font-medium text-neutral-200">{row.name}</div>
          </div>
        ),
        sortValue: (row) => row.name,
        className: "w-[20rem]",
        truncate: false,
      },
      {
        id: "description",
        header: "description",
        accessor: (row) =>
          row.description ? (
            <span
              className="text-neutral-400"
              title={row.description}
              style={
                {
                  display: "-webkit-box",
                  WebkitBoxOrient: "vertical",
                  WebkitLineClamp: 2,
                  overflow: "hidden",
                } as CSSProperties
              }
            >
              {row.description}
            </span>
          ) : (
            <span className="text-neutral-600">—</span>
          ),
        sortValue: (row) => row.description ?? "",
        truncate: false,
      },
      {
        id: "provider",
        header: "provider",
        accessor: (row) => (
          <span className="font-mono text-xs text-neutral-400">{row.provider}</span>
        ),
        sortValue: (row) => row.provider,
        className: "w-[10rem]",
      },
      {
        id: "model",
        header: "model",
        accessor: (row) => <span className="font-mono text-xs text-neutral-400">{row.model}</span>,
        sortValue: (row) => row.model,
        className: "w-[10rem]",
      },
    ],
    [],
  );

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
      <header className="flex flex-col gap-3 border-b border-neutral-800 pb-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold text-neutral-200">Agents</h1>
            <p className="text-sm text-neutral-500">
              {agents.length} agent profile{agents.length === 1 ? "" : "s"}
            </p>
          </div>
          <Link
            href="/agents/new"
            className="rounded border border-green-700 bg-green-900/50 px-4 py-1.5 text-sm font-medium text-green-300 hover:bg-green-800/50"
          >
            + New Agent
          </Link>
        </div>
      </header>

      {error ? (
        <div className="border border-red-800 bg-neutral-950 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      ) : null}

      <Table
        columns={columns}
        data={agents}
        emptyMessage={loading ? "Loading agents..." : "No agent profiles found."}
        getRowKey={(row) => row.name}
        initialSort={{ columnId: "name", direction: "asc" }}
        onRowClick={(row) => router.push(`/agents/${encodeURIComponent(row.name)}`)}
      />
    </main>
  );
}
