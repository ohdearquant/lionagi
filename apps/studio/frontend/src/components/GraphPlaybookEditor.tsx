import { Link, useNavigate } from "@tanstack/react-router";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ModelConfigTable from "@/components/ModelConfigTable";

const WorkerCanvas = lazy(() => import("@/components/canvas/WorkerCanvas"));
import { getWorkerGraph, getWorkerRaw, listAgents, updateWorker, validateWorker } from "@/lib/api";
import { IconChevronDown, IconChevronUp } from "@/components/ui/icons";
import type {
  AgentProfileSummary,
  ModelConfig,
  WorkerFormData,
  WorkerGraph,
  WorkerLinkEdge,
  WorkerStepNode,
} from "@/lib/types";

/**
 * Graph-format playbook editor — the original canvas-based editor extracted
 * into a reusable component so the route page can choose between this and
 * ``DeclarativePlaybookForm`` based on the playbook's on-disk format.
 */
export default function GraphPlaybookEditor({ workerName }: { workerName: string }) {
  const navigate = useNavigate();

  const [graph, setGraph] = useState<WorkerGraph | null>(null);
  const [description, setDescription] = useState("");
  const [models, setModels] = useState<Record<string, ModelConfig>>({});
  const [agentProfiles, setAgentProfiles] = useState<AgentProfileSummary[]>([]);

  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [showModels, setShowModels] = useState(false);

  const canvasNodesRef = useRef<WorkerStepNode[]>([]);
  const canvasEdgesRef = useRef<WorkerLinkEdge[]>([]);

  useEffect(() => {
    Promise.all([
      getWorkerGraph(workerName),
      getWorkerRaw(workerName),
      listAgents().catch(() => ({ agents: [] })),
    ])
      .then(([graphData, rawData, agentsData]) => {
        setGraph(graphData);
        setDescription(rawData.description || "");
        setModels(rawData.use?.models || {});
        setAgentProfiles(agentsData.agents);
        canvasNodesRef.current = graphData.nodes;
        canvasEdgesRef.current = graphData.edges;
      })
      .catch((err) => {
        setLoadError(err instanceof Error ? err.message : "Failed to load");
      });
  }, [workerName]);

  const allRoleNames = useMemo(() => {
    const names = new Set<string>();
    agentProfiles.forEach((p) => names.add(p.name));
    Object.keys(models).forEach((k) => names.add(k));
    return Array.from(names).sort();
  }, [agentProfiles, models]);

  const handleCanvasChange = useCallback((nodes: WorkerStepNode[], edges: WorkerLinkEdge[]) => {
    canvasNodesRef.current = nodes;
    canvasEdgesRef.current = edges;
  }, []);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setErrors([]);

    const nodes = canvasNodesRef.current;
    const edges = canvasEdgesRef.current;

    const steps: Record<
      string,
      {
        assignment: string;
        role: string;
        prompt: string;
        capacity?: number;
        timeout?: number | null;
      }
    > = {};
    for (const n of nodes) {
      steps[n.id] = {
        assignment: n.assignment,
        role: n.role,
        prompt: n.prompt,
        capacity: n.capacity,
        timeout: n.timeout,
      };
    }

    const links = edges.map((e) => ({
      from: e.source,
      to: e.target,
      condition: e.condition,
      map: e.map,
      handler: e.handler,
    }));

    const data: WorkerFormData = {
      name: workerName,
      description,
      use: { models },
      steps,
      links,
    };

    try {
      const validation = await validateWorker(workerName, data);
      if (!validation.ok) {
        setErrors(validation.errors ?? ["Validation failed"]);
        setSaving(false);
        return;
      }
      await updateWorker(workerName, data);
      void navigate({ to: "/playbooks/$name", params: { name: workerName } });
    } catch (err) {
      setErrors([err instanceof Error ? err.message : "Save failed"]);
      setSaving(false);
    }
  }, [workerName, description, models, navigate]);

  if (loadError) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-6">
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {loadError}
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

  return (
    <div className="flex h-[calc(100vh-56px)] flex-col">
      <div className="flex items-center gap-3 border-b border-edge bg-surface-nav px-4 py-2">
        <Link
          to="/playbooks/$name"
          params={{ name: workerName }}
          className="text-meta text-content-secondary hover:text-content-primary"
        >
          &larr; {workerName}
        </Link>

        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Playbook description..."
          className="flex-1 rounded-md border border-transparent bg-transparent px-2 py-1 text-body text-content-secondary placeholder:text-content-muted hover:border-edge focus:border-edge-strong focus:outline-none"
        />

        <button
          onClick={() => setShowModels((v) => !v)}
          className="rounded-md bg-interactive-secondary px-3 py-1 text-meta text-content-secondary hover:bg-interactive-secondary-hover hover:text-content-primary"
        >
          Models{" "}
          {showModels ? (
            <IconChevronUp size={9} strokeWidth={2.25} className="inline" />
          ) : (
            <IconChevronDown size={9} strokeWidth={2.25} className="inline" />
          )}
        </button>

        <button
          onClick={handleSave}
          disabled={saving}
          className="rounded-md bg-interactive-primary px-4 py-1 text-body font-medium text-content-inverse hover:bg-interactive-primary-hover disabled:cursor-not-allowed disabled:opacity-50"
        >
          {saving ? "Saving..." : "Save"}
        </button>
      </div>

      {errors.length > 0 && (
        <div className="border-b border-status-error/30 bg-status-error-bg px-4 py-2">
          {errors.map((err, i) => (
            <p key={i} className="text-meta text-status-error">
              {err}
            </p>
          ))}
        </div>
      )}

      {showModels && (
        <div className="border-b border-edge bg-surface-raised px-4 py-3">
          <div className="mx-auto max-w-3xl">
            <h3 className="mb-2 text-meta font-semibold uppercase text-content-muted">
              Model Overrides
            </h3>
            <ModelConfigTable models={models} onChange={setModels} />
          </div>
        </div>
      )}

      <div className="flex-1 min-h-0">
        <Suspense fallback={null}>
          <WorkerCanvas
            graph={graph}
            editable={true}
            roles={allRoleNames}
            agentProfiles={agentProfiles}
            modelOverrides={models}
            onChange={handleCanvasChange}
          />
        </Suspense>
      </div>
    </div>
  );
}
