"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  MarkerType,
} from "reactflow";
import type {
  Connection,
  Edge,
  Node,
  OnSelectionChangeParams,
  NodeMouseHandler,
  EdgeMouseHandler,
} from "reactflow";
import "reactflow/dist/style.css";

import StepNodeComponent from "./StepNode";
import type { StepNodeData } from "./StepNode";
import ConditionEdgeComponent from "./ConditionEdge";
import type { ConditionEdgeData } from "./ConditionEdge";
import SidePanel from "./SidePanel";
import type { Selection } from "./SidePanel";
import { getLayoutedElements } from "./useLayout";

import type {
  AgentProfileSummary,
  ModelConfig,
  WorkerGraph,
  WorkerStepNode,
  WorkerLinkEdge,
} from "@/lib/types";

// ─── Types ───────────────────────────────────────────────

interface WorkerCanvasProps {
  graph: WorkerGraph;
  editable?: boolean;
  roles?: string[];
  agentProfiles?: AgentProfileSummary[];
  modelOverrides?: Record<string, ModelConfig>;
  execSteps?: Array<{
    step: string;
    status: string;
    result?: Record<string, unknown>;
    timestamp?: number;
  }>;
  currentStep?: string | null;
  onChange?: (nodes: WorkerStepNode[], edges: WorkerLinkEdge[]) => void;
}

// ─── Conversion helpers ─────────────────────────────────

const nodeTypes = { step: StepNodeComponent };
const edgeTypes = { condition: ConditionEdgeComponent };

function toFlowNodes(nodes: WorkerStepNode[]): Node<StepNodeData>[] {
  return nodes.map((n) => ({
    id: n.id,
    type: "step",
    position: { x: 0, y: 0 },
    data: {
      label: n.label,
      role: n.role,
      assignment: n.assignment,
      prompt: n.prompt,
      capacity: n.capacity,
      timeout: n.timeout,
      inputs: n.inputs,
      outputs: n.outputs,
    },
  }));
}

function toFlowEdges(edges: WorkerLinkEdge[]): Edge<ConditionEdgeData>[] {
  return edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    type: "condition",
    data: {
      mode: e.mode,
      condition: e.condition,
      map: e.map,
      handler: e.handler,
    },
  }));
}

function fromFlowNodes(nodes: Node<StepNodeData>[]): WorkerStepNode[] {
  return nodes.map((n) => ({
    id: n.id,
    label: n.data.label,
    role: n.data.role,
    assignment: n.data.assignment,
    prompt: n.data.prompt,
    capacity: n.data.capacity,
    timeout: n.data.timeout,
    inputs: n.data.inputs,
    outputs: n.data.outputs,
  }));
}

function fromFlowEdges(edges: Edge<ConditionEdgeData>[]): WorkerLinkEdge[] {
  return edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    mode: e.data?.mode ?? "simple",
    condition: e.data?.condition,
    map: e.data?.map,
    handler: e.data?.handler,
  }));
}

// ─── Canvas ──────────────────────────────────────────────

export default function WorkerCanvas({
  graph,
  editable = false,
  roles = [],
  agentProfiles = [],
  modelOverrides = {},
  execSteps = [],
  currentStep = null,
  onChange,
}: WorkerCanvasProps) {
  const initialised = useRef(false);

  const initialFlowNodes = useMemo(() => toFlowNodes(graph.nodes), [graph.nodes]);
  const initialFlowEdges = useMemo(() => toFlowEdges(graph.edges), [graph.edges]);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [selection, setSelection] = useState<Selection>({ type: "none" });

  // Layout on mount or when graph changes
  useEffect(() => {
    const { nodes: ln, edges: le } = getLayoutedElements(initialFlowNodes, initialFlowEdges, "LR");
    setNodes(ln);
    setEdges(le);
    initialised.current = true;
  }, [initialFlowNodes, initialFlowEdges, setNodes, setEdges]);

  // Apply execution status to nodes
  useEffect(() => {
    if (execSteps.length === 0 && !currentStep) return;

    const completedMap = new Map(
      execSteps.filter((s) => s.status === "completed").map((s) => [s.step, s]),
    );

    setNodes((nds) =>
      nds.map((n) => {
        let status: StepNodeData["execStatus"] = "pending";
        if (n.id === currentStep) status = "running";
        else if (completedMap.has(n.id)) status = "completed";

        return {
          ...n,
          data: { ...n.data, execStatus: status },
        };
      }),
    );

    setEdges((eds) =>
      eds.map((e) => ({
        ...e,
        data: { ...e.data, sourceCompleted: completedMap.has(e.source) },
      })),
    );
  }, [execSteps, currentStep, setNodes, setEdges]);

  // Emit changes to parent
  useEffect(() => {
    if (!initialised.current || !onChange) return;
    onChange(fromFlowNodes(nodes), fromFlowEdges(edges));
  }, [nodes, edges, onChange]);

  // Node click
  const onNodeClick: NodeMouseHandler = useCallback(
    (_event, node) => {
      const typedNode = node as Node<StepNodeData>;
      const execResult = execSteps.find((s) => s.step === typedNode.id && s.status === "completed");

      if (execResult?.result) {
        setSelection({
          type: "exec-result",
          id: typedNode.id,
          data: typedNode.data,
          result: execResult.result,
        });
      } else {
        setSelection({ type: "node", id: typedNode.id, data: typedNode.data });
      }
    },
    [execSteps],
  );

  // Edge click
  const onEdgeClick: EdgeMouseHandler = useCallback((_event, edge) => {
    const typedEdge = edge as Edge<ConditionEdgeData>;
    if (typedEdge.data) {
      setSelection({ type: "edge", id: typedEdge.id, data: typedEdge.data });
    }
  }, []);

  // Pane click — deselect
  const onPaneClick = useCallback(() => {
    setSelection({ type: "none" });
  }, []);

  // Connect new edge
  const onConnect = useCallback(
    (connection: Connection) => {
      if (!editable) return;
      const newEdge: Edge<ConditionEdgeData> = {
        ...connection,
        id: `e-${connection.source}-${connection.target}`,
        type: "condition",
        data: { mode: "simple" },
      } as Edge<ConditionEdgeData>;
      setEdges((eds) => addEdge(newEdge, eds));
    },
    [editable, setEdges],
  );

  // Node update from side panel
  const onNodeUpdate = useCallback(
    (id: string, data: Partial<StepNodeData>) => {
      setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...data } } : n)));
      setSelection((prev) =>
        prev.type === "node" && prev.id === id
          ? { ...prev, data: { ...prev.data, ...data } }
          : prev,
      );
    },
    [setNodes],
  );

  // Edge update from side panel
  const onEdgeUpdate = useCallback(
    (id: string, data: Partial<ConditionEdgeData>) => {
      setEdges((eds) => eds.map((e) => (e.id === id ? { ...e, data: { ...e.data, ...data } } : e)));
      setSelection((prev) =>
        prev.type === "edge" && prev.id === id
          ? { ...prev, data: { ...prev.data, ...data } as ConditionEdgeData }
          : prev,
      );
    },
    [setEdges],
  );

  // Delete node or edge
  const onDeleteElement = useCallback(
    (type: "node" | "edge", id: string) => {
      if (type === "node") {
        setNodes((nds) => nds.filter((n) => n.id !== id));
        setEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id));
      } else {
        setEdges((eds) => eds.filter((e) => e.id !== id));
      }
      setSelection({ type: "none" });
    },
    [setNodes, setEdges],
  );

  // Add new step
  const onAddStep = useCallback(() => {
    const existing = nodes.map((n) => n.id);
    let num = existing.length + 1;
    while (existing.includes(`step_${num}`)) num++;
    const name = `step_${num}`;

    const newNode: Node<StepNodeData> = {
      id: name,
      type: "step",
      position: { x: nodes.length * 290 + 40, y: 100 },
      data: {
        label: name,
        role: "",
        assignment: "",
        prompt: "",
        capacity: 1,
        timeout: null,
        inputs: [],
        outputs: [],
      },
    };
    setNodes((nds) => [...nds, newNode]);
    setSelection({ type: "node", id: name, data: newNode.data });
  }, [nodes, setNodes]);

  // Auto layout
  const handleAutoLayout = useCallback(() => {
    const { nodes: ln, edges: le } = getLayoutedElements(nodes, edges, "LR");
    setNodes(ln);
    setEdges(le);
  }, [nodes, edges, setNodes, setEdges]);

  return (
    <div className="flex h-full">
      {/* Canvas */}
      <div className="relative flex-1">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={editable ? onNodesChange : undefined}
          onEdgesChange={editable ? onEdgesChange : undefined}
          onConnect={onConnect}
          onNodeClick={onNodeClick}
          onEdgeClick={onEdgeClick}
          onPaneClick={onPaneClick}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          nodesDraggable={true}
          nodesConnectable={editable}
          elementsSelectable={true}
          fitView
          fitViewOptions={{ padding: 0.3 }}
          proOptions={{ hideAttribution: true }}
          className="bg-surface-base"
        >
          <Background color="var(--edge-subtle)" gap={20} size={1} />
          <Controls
            showInteractive={false}
            className="!bg-surface-raised !border-edge !shadow-none [&>button]:!bg-surface-raised [&>button]:!border-edge [&>button]:!text-content-secondary [&>button:hover]:!bg-surface-overlay [&>button:hover]:!text-content-primary"
          />
          {nodes.length > 10 ? (
            <MiniMap
              nodeColor={() => "var(--edge-strong)"}
              maskColor="rgba(0, 0, 0, 0.5)"
              className="!bg-surface-raised !border-edge"
            />
          ) : null}

          {/* Custom SVG markers */}
          <svg>
            <defs>
              <marker id="arrow" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
                <polygon points="0 0, 8 3, 0 6" fill="var(--edge-strong)" />
              </marker>
              <marker
                id="arrow-active"
                markerWidth="8"
                markerHeight="6"
                refX="8"
                refY="3"
                orient="auto"
              >
                <polygon points="0 0, 8 3, 0 6" fill="var(--status-success)" />
              </marker>
            </defs>
          </svg>
        </ReactFlow>

        {/* Toolbar */}
        {editable && (
          <div className="absolute bottom-4 left-4 flex items-center gap-2 z-10">
            <button
              onClick={onAddStep}
              className="rounded-md bg-interactive-secondary px-3 py-1.5 text-xs font-medium text-content-primary hover:bg-interactive-secondary-hover"
            >
              + Add Step
            </button>
            <button
              onClick={handleAutoLayout}
              className="rounded-md bg-interactive-secondary px-3 py-1.5 text-xs font-medium text-content-primary hover:bg-interactive-secondary-hover"
            >
              Auto Layout
            </button>
          </div>
        )}
      </div>

      {/* Side Panel */}
      <div className="w-80 shrink-0 border-l border-edge bg-surface-overlay overflow-y-auto">
        <SidePanel
          selection={selection}
          editable={editable}
          roles={roles}
          agentProfiles={agentProfiles}
          modelOverrides={modelOverrides}
          onNodeUpdate={onNodeUpdate}
          onEdgeUpdate={onEdgeUpdate}
          onDelete={onDeleteElement}
        />
      </div>
    </div>
  );
}
