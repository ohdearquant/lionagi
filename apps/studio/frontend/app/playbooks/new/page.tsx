"use client";

import Link from "next/link";
import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ModelConfigTable from "@/components/ModelConfigTable";

const WorkerCanvas = dynamic(() => import("@/components/canvas/WorkerCanvas"), { ssr: false });
import { createWorker, listAgents, validateWorker } from "@/lib/api";
import type {
  AgentProfileSummary,
  ModelConfig,
  WorkerFormData,
  WorkerGraph,
  WorkerLinkEdge,
  WorkerStepNode,
} from "@/lib/types";

const EMPTY_GRAPH: WorkerGraph = {
  name: "",
  description: "",
  nodes: [
    {
      id: "step_1",
      label: "step_1",
      assignment: "",
      role: "",
      prompt: "",
      capacity: 1,
      timeout: null,
      inputs: [],
      outputs: [],
    },
  ],
  edges: [],
};

export default function NewWorkerPage() {
  const router = useRouter();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [models, setModels] = useState<Record<string, ModelConfig>>({});
  const [agentProfiles, setAgentProfiles] = useState<AgentProfileSummary[]>([]);

  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [showModels, setShowModels] = useState(false);

  const canvasNodesRef = useRef<WorkerStepNode[]>(EMPTY_GRAPH.nodes);
  const canvasEdgesRef = useRef<WorkerLinkEdge[]>([]);

  useEffect(() => {
    listAgents()
      .then((data) => setAgentProfiles(data.agents))
      .catch(() => {});
  }, []);

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
    if (!name.trim()) {
      setErrors(["Playbook name is required"]);
      return;
    }

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
      name: name.trim(),
      description,
      use: { models },
      steps,
      links,
    };

    try {
      const validation = await validateWorker(data.name, data);
      if (!validation.ok) {
        setErrors(validation.errors ?? ["Validation failed"]);
        setSaving(false);
        return;
      }
      await createWorker(data.name, data);
      router.push(`/playbooks/${encodeURIComponent(data.name)}`);
    } catch (err) {
      setErrors([err instanceof Error ? err.message : "Create failed"]);
      setSaving(false);
    }
  }, [name, description, models, router]);

  return (
    <div className="flex h-[calc(100vh-56px)] flex-col">
      {/* Top bar */}
      <div className="flex items-center gap-3 border-b border-neutral-800 px-4 py-2">
        <Link href="/playbooks" className="text-xs text-neutral-500 hover:text-neutral-300">
          &larr; playbooks
        </Link>

        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Playbook name..."
          className="w-48 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 font-mono text-sm text-neutral-200 placeholder-neutral-600 focus:border-neutral-500 focus:outline-none"
        />

        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Description..."
          className="flex-1 rounded border border-transparent bg-transparent px-2 py-1 text-sm text-neutral-400 placeholder-neutral-600 hover:border-neutral-700 focus:border-neutral-500 focus:outline-none"
        />

        <button
          onClick={() => setShowModels((v) => !v)}
          className="rounded border border-neutral-700 bg-neutral-900 px-3 py-1 text-xs text-neutral-400 hover:text-neutral-200"
        >
          Models {showModels ? "▴" : "▾"}
        </button>

        <button
          onClick={handleSave}
          disabled={saving || !name.trim()}
          className="rounded border border-green-700 bg-green-900/50 px-4 py-1 text-sm font-medium text-green-300 hover:bg-green-800/50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {saving ? "Creating..." : "Create"}
        </button>
      </div>

      {/* Errors */}
      {errors.length > 0 && (
        <div className="border-b border-red-900 bg-red-950/40 px-4 py-2">
          {errors.map((err, i) => (
            <p key={i} className="text-xs text-red-300">
              {err}
            </p>
          ))}
        </div>
      )}

      {/* Model overrides (collapsible) */}
      {showModels && (
        <div className="border-b border-neutral-800 bg-neutral-950 px-4 py-3">
          <div className="mx-auto max-w-3xl">
            <h3 className="mb-2 text-xs font-semibold uppercase text-neutral-500">
              Model Overrides
            </h3>
            <ModelConfigTable models={models} onChange={setModels} />
          </div>
        </div>
      )}

      {/* Canvas */}
      <div className="flex-1 min-h-0">
        <WorkerCanvas
          graph={EMPTY_GRAPH}
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
