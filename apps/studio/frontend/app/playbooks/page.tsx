"use client";

import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import Table, { type TableColumn } from "@/components/Table";
import { listWorkers } from "@/lib/api";
import type { WorkerSummary } from "@/lib/types";

function messageFromError(error: unknown): string {
  return error instanceof Error ? error.message : "Failed to load playbooks";
}

export default function WorkersPage() {
  const router = useRouter();
  const [workers, setWorkers] = useState<WorkerSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    async function loadWorkers() {
      try {
        const response = await listWorkers();
        if (active) {
          setWorkers(response.workers);
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

    void loadWorkers();

    return () => {
      active = false;
    };
  }, []);

  const columns = useMemo<Array<TableColumn<WorkerSummary>>>(
    () => [
      {
        id: "name",
        header: "name",
        accessor: (row) => (
          <div className="min-w-0">
            <div className="truncate font-medium text-neutral-200">{row.name}</div>
            {row.file ? <div className="truncate text-xs text-neutral-500">{row.file}</div> : null}
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
        id: "steps",
        header: "steps",
        accessor: (row) => row.steps,
        sortValue: (row) => row.steps,
        align: "right",
        className: "w-[5rem]",
      },
      {
        id: "links",
        header: "links",
        accessor: (row) => row.links,
        sortValue: (row) => row.links,
        align: "right",
        className: "w-[5rem]",
      },
    ],
    [],
  );

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
      <header className="flex flex-col gap-3 border-b border-neutral-800 pb-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold text-neutral-200">Playbooks</h1>
            <p className="text-sm text-neutral-500">
              {workers.length} playbook definition{workers.length === 1 ? "" : "s"}
            </p>
          </div>
          <Link
            href="/playbooks/new"
            className="rounded border border-green-700 bg-green-900/50 px-4 py-1.5 text-sm font-medium text-green-300 hover:bg-green-800/50"
          >
            + New Playbook
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
        data={workers}
        emptyMessage={loading ? "Loading playbooks..." : "No playbook definitions found."}
        getRowKey={(row) => row.name}
        initialSort={{ columnId: "name", direction: "asc" }}
        onRowClick={(row) => router.push(`/playbooks/${encodeURIComponent(row.name)}`)}
      />
    </main>
  );
}
