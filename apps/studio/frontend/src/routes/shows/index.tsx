import { createFileRoute, redirect, useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import Badge from "@/components/Badge";
import Table, { type TableColumn } from "@/components/Table";
import { listShows } from "@/lib/api";
import type { ShowSummary } from "@/lib/types";
import { empty } from "@/lib/copy";

export const Route = createFileRoute("/shows/")({
  beforeLoad: () => {
    throw redirect({ to: "/", search: (prev) => ({ ...prev, source: "script" }) });
  },
  component: ShowsPage,
});

function formatLastUpdate(ts: number | string | null): string {
  if (!ts) return "—";
  if (typeof ts === "number") return new Date(ts * 1000).toLocaleString();
  return new Date(ts).toLocaleString();
}

function ShowsPage() {
  const navigate = useNavigate();
  const [shows, setShows] = useState<ShowSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        const data = await listShows();
        if (active) setShows(data);
      } catch {
        if (active) setShows([]);
      } finally {
        if (active) setLoading(false);
      }
    }

    void load();
    const interval = setInterval(load, 5000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, []);

  const columns = useMemo<Array<TableColumn<ShowSummary>>>(
    () => [
      {
        id: "topic",
        header: "Topic",
        accessor: (row) => (
          <span className="font-mono text-body text-status-running">{row.topic}</span>
        ),
        sortValue: (row) => row.topic,
        className: "w-[20rem]",
        truncate: false,
      },
      {
        id: "play_count",
        header: "Plays",
        key: "play_count",
        sortValue: (row) => row.play_count,
        align: "right",
        className: "w-20",
      },
      {
        id: "latest_status",
        header: "Latest Status",
        accessor: (row) => (
          <Badge value={row.latest_status || "—"}>{row.latest_status || "—"}</Badge>
        ),
        sortValue: (row) => row.latest_status,
        className: "w-[10rem]",
      },
      {
        id: "last_update",
        header: "Last Update",
        accessor: (row) => (
          <span className="text-meta text-content-muted">{formatLastUpdate(row.last_update)}</span>
        ),
        sortValue: (row) =>
          typeof row.last_update === "number"
            ? row.last_update
            : row.last_update
              ? new Date(row.last_update).getTime() / 1000
              : 0,
        className: "w-[12rem]",
      },
    ],
    [],
  );

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
      <header className="flex flex-col gap-3 border-b border-edge pb-4">
        <div>
          <h1 className="text-xl font-semibold text-content-primary">Shows</h1>
          <p className="text-body text-content-muted">
            {shows.length} show{shows.length !== 1 ? "s" : ""}
          </p>
        </div>
      </header>

      <Table
        data={shows}
        columns={columns}
        emptyMessage={loading ? empty.loadingShows : empty.shows}
        getRowKey={(row) => row.topic}
        onRowClick={(row) => void navigate({ to: "/shows/$topic", params: { topic: row.topic } })}
      />
    </main>
  );
}
