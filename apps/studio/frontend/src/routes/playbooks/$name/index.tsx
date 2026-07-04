import { createFileRoute, Link } from "@tanstack/react-router";
import { lazy, Suspense, useEffect, useState } from "react";
import Button from "@/components/Button";
import { getWorkerGraph } from "@/lib/api";
import { notImplemented } from "@/lib/copy";
import type { WorkerGraph } from "@/lib/types";

const WorkerCanvas = lazy(() => import("@/components/canvas/WorkerCanvas"));

export const Route = createFileRoute("/playbooks/$name/")({
  component: WorkerDetailPage,
});

// ADR-0014: Run button is defaults-only. No task input, no CWD field.
// Input variable binding and worktree customisation belong in `li play`.
//
// POST /api/playbooks/{name}/run is not implemented on the backend (it
// returns 501), so there is no run to track progress for yet. The Run
// button stays disabled and points operators at the CLI instead of
// simulating a run state that never actually happens.

function WorkerDetailPage() {
  const { name } = Route.useParams();
  const workerName = name;
  const [graph, setGraph] = useState<WorkerGraph | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getWorkerGraph(workerName)
      .then((data) => {
        if (active) setGraph(data);
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : "Failed to load");
      });
    return () => {
      active = false;
    };
  }, [workerName]);

  if (error) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-6">
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {error}
        </div>
      </main>
    );
  }

  if (!graph) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-6">
        <p className="text-body text-content-muted">Loading...</p>
      </main>
    );
  }

  const runDisabled = true;

  return (
    <div className="flex h-[calc(100vh-56px)] flex-col">
      {/* Top bar */}
      <div className="flex items-center gap-3 border-b border-edge bg-surface-nav px-4 py-2">
        <Link to="/playbooks" className="text-meta text-content-muted hover:text-content-primary">
          playbooks
        </Link>
        <span className="text-meta text-content-muted">/</span>
        <span className="font-mono text-label font-semibold text-content-primary">
          {graph.name}
        </span>
        {graph.description ? (
          <span className="truncate text-body text-content-secondary">— {graph.description}</span>
        ) : null}

        <div className="ml-auto flex items-center gap-2">
          <Button
            variant="primary"
            size="sm"
            disabled={runDisabled}
            title={notImplemented.runPlaybook}
            leading="▶"
          >
            Run
          </Button>
          <Link to="/playbooks/$name/edit" params={{ name: workerName }}>
            <Button variant="secondary" size="sm" leading="✎">
              Edit
            </Button>
          </Link>
        </div>
      </div>

      {/* Canvas — or empty state when no steps */}
      {graph.nodes.length === 0 ? (
        <div className="flex flex-1 items-center justify-center bg-surface-base px-4 py-10">
          <PlaybookEmptyState workerName={workerName} runDisabled={runDisabled} />
        </div>
      ) : (
        <div className="min-h-0 flex-1">
          <Suspense fallback={null}>
            <WorkerCanvas graph={graph} editable={false} />
          </Suspense>
        </div>
      )}
    </div>
  );
}

function PlaybookEmptyState({
  workerName,
  runDisabled,
}: {
  workerName: string;
  runDisabled: boolean;
}) {
  return (
    <div className="flex max-w-xl flex-col gap-4 rounded-lg border border-edge bg-surface-raised p-6 text-center">
      <div className="flex items-center justify-center gap-2 text-content-muted">
        <span aria-hidden className="text-2xl">
          ◇
        </span>
      </div>
      <div>
        <h2 className="text-label font-semibold text-content-primary">No steps defined yet</h2>
        <p className="mt-1 text-body text-content-secondary">
          This playbook uses the declarative agent + prompt format. Run it with defaults or open the
          editor to author steps as a graph.
        </p>
      </div>
      <div className="flex flex-wrap justify-center gap-2">
        <Button
          variant="primary"
          size="md"
          disabled={runDisabled}
          title={notImplemented.runPlaybook}
          leading="▶"
        >
          Run
        </Button>
        <Link to="/playbooks/$name/edit" params={{ name: workerName }}>
          <Button variant="secondary" size="md" leading="✎">
            Open editor
          </Button>
        </Link>
      </div>
    </div>
  );
}
