"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import WorkerCanvas from "@/components/canvas/WorkerCanvas";
import ModelConfigTable from "@/components/ModelConfigTable";
import { getWorkerGraph, getWorkerRaw, listAgents, updateWorker, validateWorker } from "@/lib/api";
import type {
  AgentProfileSummary,
  ModelConfig,
  WorkerFormData,
  WorkerGraph,
  WorkerLinkEdge,
  WorkerStepNode,
} from "@/lib/types";

export default function EditWorkerPage({ params }: { params: { name: string } }) {
  const workerName = decodeURIComponent(params.name);
  const router = useRouter();

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
      router.push(`/playbooks/${encodeURIComponent(workerName)}`);
    } catch (err) {
      setErrors([err instanceof Error ? err.message : "Save failed"]);
      setSaving(false);
    }
  }, [workerName, description, models, router]);

  if (loadError) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-6">
        <div className="border border-red-800 bg-neutral-950 px-3 py-2 text-sm text-red-300">
          {loadError}
        </div>
      </main>
    );
  }

  if (!graph) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-6">
        <p className="text-sm text-neutral-500">Loading...</p>
      </main>
    );
  }

  return (
    <div className="flex h-[calc(100vh-56px)] flex-col">
      {/* Top bar */}
      <div className="flex items-center gap-3 border-b border-edge bg-surface-nav px-4 py-2">
        <Link
          href={`/playbooks/${encodeURIComponent(workerName)}`}
          className="text-xs text-content-secondary hover:text-content-primary"
        >
          &larr; {workerName}
        </Link>

        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Playbook description..."
          className="flex-1 rounded-md border border-transparent bg-transparent px-2 py-1 text-sm text-content-secondary placeholder-content-muted hover:border-edge focus:border-edge-strong focus:outline-none"
        />

        <button
          onClick={() => setShowModels((v) => !v)}
          className="rounded-md bg-interactive-secondary px-3 py-1 text-xs text-content-secondary hover:text-content-primary hover:bg-interactive-secondary-hover"
        >
          Models {showModels ? "▴" : "▾"}
        </button>

        <button
          onClick={handleSave}
          disabled={saving}
          className="rounded-md bg-interactive-primary px-4 py-1 text-sm font-medium text-content-inverse hover:bg-interactive-primary-hover disabled:cursor-not-allowed disabled:opacity-50"
        >
          {saving ? "Saving..." : "Save"}
        </button>
      </div>

      {/* Errors */}
      {errors.length > 0 && (
        <div className="border-b border-status-error/30 bg-status-error-bg px-4 py-2">
          {errors.map((err, i) => (
            <p key={i} className="text-xs text-status-error">
              {err}
            </p>
          ))}
        </div>
      )}

      {/* Model overrides (collapsible) */}
      {showModels && (
        <div className="border-b border-edge bg-surface-raised px-4 py-3">
          <div className="mx-auto max-w-3xl">
            <h3 className="mb-2 text-xs font-semibold uppercase text-content-muted">
              Model Overrides
            </h3>
            <ModelConfigTable models={models} onChange={setModels} />
          </div>
        </div>
      )}

      {/* Canvas */}
      <div className="flex-1 min-h-0">
        <WorkerCanvas
          graph={graph}
          editable={true}
          roles={allRoleNames}
          agentProfiles={agentProfiles}
          modelOverrides={models}
          onChange={handleCanvasChange}
        />
      </div>
    </div>
  );
}
