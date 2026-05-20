"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import Button from "@/components/Button";
import PageHeader from "@/components/PageHeader";
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

  const hasDescriptions = useMemo(
    () => workers.some((w) => w.description && w.description.trim().length > 0),
    [workers],
  );

  const columns = useMemo<Array<TableColumn<WorkerSummary>>>(() => {
    const base: Array<TableColumn<WorkerSummary>> = [
      {
        id: "name",
        header: "Name",
        accessor: (row) => (
          <div className="min-w-0">
            <div className="truncate font-medium text-content-primary">{row.name}</div>
            {row.file ? (
              <div className="truncate text-meta text-content-muted">{row.file}</div>
            ) : null}
          </div>
        ),
        sortValue: (row) => row.name,
        className: hasDescriptions ? "w-[18rem]" : "w-[24rem]",
        truncate: false,
      },
    ];

    if (hasDescriptions) {
      base.push({
        id: "description",
        header: "Description",
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

    base.push(
      {
        id: "steps",
        header: "Steps",
        accessor: (row) =>
          row.steps > 0 ? (
            <span className="tabular-nums text-content-primary">{row.steps}</span>
          ) : (
            <span className="text-content-muted">—</span>
          ),
        sortValue: (row) => row.steps,
        align: "right",
        className: "w-[6rem] tabular-nums",
      },
      {
        id: "links",
        header: "Links",
        accessor: (row) =>
          row.links > 0 ? (
            <span className="tabular-nums text-content-primary">{row.links}</span>
          ) : (
            <span className="text-content-muted">—</span>
          ),
        sortValue: (row) => row.links,
        align: "right",
        className: "w-[6rem] tabular-nums",
      },
    );

    return base;
  }, [hasDescriptions]);

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
      <PageHeader
        title="Playbooks"
        subtitle={`${workers.length} playbook definition${workers.length === 1 ? "" : "s"}`}
        actions={
          <Link href="/playbooks/new">
            <Button variant="primary" size="sm" leading="+">
              New Playbook
            </Button>
          </Link>
        }
      />

      {error ? (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
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
