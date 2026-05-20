"use client";

import { useEffect, useMemo, useState } from "react";
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

  const hasDescriptions = useMemo(
    () => agents.some((row) => row.description && row.description.trim().length > 0),
    [agents],
  );

  const columns = useMemo<Array<TableColumn<AgentProfileSummary>>>(() => {
    const cols: Array<TableColumn<AgentProfileSummary>> = [
      {
        id: "name",
        header: "name",
        accessor: (row) => (
          <div className="min-w-0">
            <div className="truncate font-medium text-content-primary">{row.name}</div>
          </div>
        ),
        sortValue: (row) => row.name,
        className: hasDescriptions ? "w-[18rem]" : "w-[24rem]",
        truncate: false,
      },
    ];

    if (hasDescriptions) {
      cols.push({
        id: "description",
        header: "description",
        accessor: (row) =>
          row.description ? (
            <span
              className="text-content-secondary"
              title={row.description}
              style={{
                display: "-webkit-box",
                WebkitBoxOrient: "vertical",
                WebkitLineClamp: 2,
                overflow: "hidden",
              }}
            >
              {row.description}
            </span>
          ) : (
            <span className="text-content-muted">—</span>
          ),
        sortValue: (row) => row.description ?? "",
        truncate: false,
      });
    }

    cols.push(
      {
        id: "provider",
        header: "provider",
        accessor: (row) => (
          <span className="font-mono text-meta text-content-secondary">{row.provider}</span>
        ),
        sortValue: (row) => row.provider,
        className: "w-[12rem]",
      },
      {
        id: "model",
        header: "model",
        accessor: (row) => (
          <span className="font-mono text-meta text-content-secondary">{row.model}</span>
        ),
        sortValue: (row) => row.model,
        className: "w-[14rem]",
      },
    );

    return cols;
  }, [hasDescriptions]);

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
      <header className="flex flex-col gap-3 border-b border-edge pb-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold text-content-primary">Agents</h1>
            <p className="text-body text-content-muted">
              {agents.length} agent profile{agents.length === 1 ? "" : "s"}
            </p>
          </div>
          <Link
            href="/agents/new"
            className="rounded border border-interactive-primary/40 bg-status-success-bg px-3 py-1.5 text-body font-medium text-status-success hover:border-interactive-primary hover:bg-interactive-primary hover:text-content-inverse"
          >
            + New Agent
          </Link>
        </div>
      </header>

      {error ? (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
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
