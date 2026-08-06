"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
} from "reactflow";
import type {
  Connection,
  Edge,
  Node,
  NodeMouseHandler,
  EdgeMouseHandler,
  ReactFlowInstance,
} from "reactflow";
import "reactflow/dist/style.css";

import StepNodeComponent from "./StepNode";
import type { StepNodeData, NodeExecStatus } from "./StepNode";
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

// ─── Readability floor ───────────────────────────────────
//
// fitView shrinks the whole graph to fit the container, with no regard for
// whether the result is still legible. StepNode's smallest text (label,
// role, assignment, stats rows) all render at --t-xs (11px, theme.css) —
// ConditionEdge's condition chip matches. Below a 7px screen size even
// anti-aliased text stops being legible, so the floor is the zoom at which
// an 11px glyph lands on 7px: 7 / 11 = 0.636, rounded up to 0.65 for a small
// margin. Below the floor the canvas overflows its container instead of
// shrinking further; ReactFlow's own pan/zoom-out takes over from there.
export const FIT_ZOOM_FLOOR = 0.65;

// Computed fit zoom for a laid-out graph in a given viewport — the same
// arithmetic ReactFlow's fitView/getViewportForBounds uses internally (fit
// width and height under a SINGLE padding term, then clamp to [minZoom,
// maxZoom], take the smaller axis). Exported so layout fixtures can assert
// "this graph's fit zoom would clear the floor" without mounting ReactFlow.
// minZoom defaults to FIT_ZOOM_FLOOR because that is the clamp WorkerCanvas
// actually wires into <ReactFlow minZoom>/fitViewOptions below — callers that
// need the pre-clamp raw arithmetic (e.g. to demonstrate why the clamp is
// needed) can pass 0 explicitly.
export function fitZoomFor(
  graphWidth: number,
  graphHeight: number,
  viewportWidth: number,
  viewportHeight: number,
  padding: number,
  maxZoom: number,
  minZoom: number = FIT_ZOOM_FLOOR,
): number {
  const w = viewportWidth / (graphWidth * (1 + padding)) || 1;
  const h = viewportHeight / (graphHeight * (1 + padding)) || 1;
  return Math.min(Math.max(Math.min(w, h), minZoom), maxZoom);
}

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
  /** Authored step id → live lifecycle status, correlated from Node* signals
   * (never from op_id — see lib/operationGraph.ts buildNodeStatusesByName).
   * Takes priority over execSteps/currentStep for node coloring when a node
   * has a matching entry; nodes with no entry fall back to the legacy
   * execSteps/currentStep-derived status. */
  nodeStatuses?: Record<string, NodeExecStatus>;
  currentStep?: string | null;
  onChange?: (nodes: WorkerStepNode[], edges: WorkerLinkEdge[]) => void;
  /** Read-only embed in a small container (e.g. RunDetail's 280px run-dag
   * panel). Suppresses the MiniMap — at that size it reads as a floating
   * cluster of gray nodes rather than a useful overview. */
  compact?: boolean;
  /** Reports the laid-out graph's bounding-box height (px) after each layout,
   * so an embedding container can size itself to the graph's real shape
   * instead of guessing from node count. */
  onLayoutHeight?: (height: number) => void;
}

// ─── Conversion helpers ─────────────────────────────────

const nodeTypes = { step: StepNodeComponent };
const edgeTypes = { condition: ConditionEdgeComponent };

// Width of the details side panel (the w-80 strips below). The read-only
// overlay variant covers this much of the canvas's right edge, and the
// pan-clear-of-panel logic keys off the same number.
const SIDE_PANEL_WIDTH = 320;

// How far left the viewport must shift for a node to clear the side-panel
// strip, in screen pixels — 0 when it is already clear. Node coordinates are
// graph-space; the viewport transform maps them to screen space.
export function panelClearanceShift(
  nodeX: number,
  nodeWidth: number,
  viewport: { x: number; zoom: number },
  containerWidth: number,
  panelWidth: number = SIDE_PANEL_WIDTH,
): number {
  const panelLeft = containerWidth - panelWidth;
  const nodeRight = (nodeX + nodeWidth) * viewport.zoom + viewport.x;
  return nodeRight > panelLeft ? nodeRight - panelLeft + 16 : 0;
}

// nodeStatuses only covers nodes it has live signal correlation for — a
// legacy run (no matching signals, or none at all) still passes a truthy
// object (RunDetail always builds one when a planned graph exists, `{}` in
// the legacy case). An edge's source node absent from that map must fall
// back to the legacy execSteps-derived completedMap rather than being
// treated as "not completed" just because *some* nodeStatuses object exists.
// A MiniMap only earns its keep once the canvas is large enough for an
// overview to mean something. In a `compact` embed (RunDetail's 280px
// run-dag panel) it instead reads as a floating cluster of gray micro-nodes
// overlapping the real graph, so suppress it outright there regardless of
// node count.
export function shouldShowMiniMap(compact: boolean, nodeCount: number): boolean {
  if (compact) return false;
  return nodeCount > 10;
}

// The side panel is an editor surface. In a read-only embed with nothing
// selected it is 320px of "click a step to inspect" placeholder — a quarter
// of the canvas spent saying nothing — so it appears only once there is a
// selection to show. The editor keeps it permanently, since add/edit flows
// live there.
export function shouldShowSidePanel(editable: boolean, selectionType: Selection["type"]): boolean {
  return editable || selectionType !== "none";
}

export function computeEdgeSourceCompleted(
  source: string,
  nodeStatuses: Record<string, NodeExecStatus> | undefined,
  completedMap: Map<string, unknown>,
): boolean {
  const live = nodeStatuses?.[source];
  return live !== undefined ? live === "completed" : completedMap.has(source);
}

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

// Rank distance is a layout output (useLayout's rank map), not something
// toFlowEdges can know at the initial graph -> ReactFlow conversion — so it
// is stamped on afterward, once a layout pass has run. Edges outside the
// rank map (e.g. a node dropped mid-edit) fall back to undefined, which
// ConditionEdge treats as short-range.
function attachRankDistance(
  edges: Edge<ConditionEdgeData>[],
  ranks: Map<string, number>,
): Edge<ConditionEdgeData>[] {
  return edges.map((e) => {
    const srcRank = ranks.get(e.source);
    const tgtRank = ranks.get(e.target);
    const rankDistance =
      srcRank !== undefined && tgtRank !== undefined ? tgtRank - srcRank : undefined;
    return { ...e, data: { ...(e.data as ConditionEdgeData), rankDistance } };
  });
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
  nodeStatuses,
  currentStep = null,
  onChange,
  compact = false,
  onLayoutHeight,
}: WorkerCanvasProps) {
  const initialised = useRef(false);

  const initialFlowNodes = useMemo(() => toFlowNodes(graph.nodes), [graph.nodes]);
  const initialFlowEdges = useMemo(() => toFlowEdges(graph.edges), [graph.edges]);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [selection, setSelection] = useState<Selection>({ type: "none" });

  // The fitView PROP fits once, on init — before an async graph load has laid
  // anything out, and before an embedding container has grown to the layout's
  // reported height. Both arrive later, so the fit is re-run from the
  // instance when the laid-out nodes land and when the container resizes.
  const flowRef = useRef<ReactFlowInstance | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const refitRaf = useRef<number | null>(null);
  const refit = useCallback(() => {
    // One pending frame at a time: a burst of resize callbacks coalesces into
    // a single fit, and the handle lets unmount cancel a fit that would
    // otherwise run against a disposed instance.
    if (refitRaf.current !== null) cancelAnimationFrame(refitRaf.current);
    refitRaf.current = requestAnimationFrame(() => {
      refitRaf.current = null;
      flowRef.current?.fitView({ padding: 0.15, maxZoom: 1, minZoom: FIT_ZOOM_FLOOR });
    });
  }, []);
  useEffect(() => {
    return () => {
      if (refitRaf.current !== null) cancelAnimationFrame(refitRaf.current);
    };
  }, []);
  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(refit);
    observer.observe(el);
    return () => observer.disconnect();
  }, [refit]);

  // Layout on mount or when graph changes
  useEffect(() => {
    const {
      nodes: ln,
      edges: le,
      height,
      ranks,
    } = getLayoutedElements(initialFlowNodes, initialFlowEdges, "LR");
    setNodes(ln);
    setEdges(attachRankDistance(le, ranks));
    initialised.current = true;
    onLayoutHeight?.(height);
    refit();
  }, [initialFlowNodes, initialFlowEdges, setNodes, setEdges, onLayoutHeight, refit]);

  // Apply execution status to nodes. nodeStatuses (live signal-derived, keyed
  // by authored step id) takes priority per node; nodes it doesn't cover fall
  // back to the legacy execSteps/currentStep derivation.
  useEffect(() => {
    if (execSteps.length === 0 && !currentStep && !nodeStatuses) return;

    const completedMap = new Map(
      execSteps.filter((s) => s.status === "completed").map((s) => [s.step, s]),
    );

    setNodes((nds) =>
      nds.map((n) => {
        let status: StepNodeData["execStatus"] = "pending";
        const live = nodeStatuses?.[n.id];
        if (live) status = live;
        else if (n.id === currentStep) status = "running";
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
        data: {
          ...e.data,
          sourceCompleted: computeEdgeSourceCompleted(e.source, nodeStatuses, completedMap),
        },
      })),
    );
  }, [execSteps, currentStep, nodeStatuses, setNodes, setEdges]);

  // Emit changes to parent
  useEffect(() => {
    if (!initialised.current || !onChange) return;
    onChange(fromFlowNodes(nodes), fromFlowEdges(edges));
  }, [nodes, edges, onChange]);

  // In read-only embeds the side panel is an absolute overlay on the right
  // edge of the canvas, so a click on a node under that strip would summon a
  // panel that hides the very node it describes. Pan the node clear first;
  // the editable panel is a flex sibling instead, whose mount resizes the
  // canvas and re-fits through the ResizeObserver.
  const panClearOfPanel = useCallback((node: Node) => {
    const instance = flowRef.current;
    const container = containerRef.current;
    if (!instance || !container) return;
    const { x, y, zoom } = instance.getViewport();
    const shift = panelClearanceShift(
      node.position.x,
      node.width ?? 210,
      { x, zoom },
      container.clientWidth,
    );
    if (shift > 0) {
      instance.setViewport({ x: x - shift, y, zoom }, { duration: 250 });
    }
  }, []);

  // Node click
  const onNodeClick: NodeMouseHandler = useCallback(
    (_event, node) => {
      const typedNode = node as Node<StepNodeData>;
      const execResult = execSteps.find((s) => s.step === typedNode.id && s.status === "completed");

      if (!editable) panClearOfPanel(node);
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
    [execSteps, editable, panClearOfPanel],
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
    const { nodes: ln, edges: le, ranks } = getLayoutedElements(nodes, edges, "LR");
    setNodes(ln);
    setEdges(attachRankDistance(le, ranks));
  }, [nodes, edges, setNodes, setEdges]);

  return (
    <div className="relative flex h-full">
      {/* Canvas */}
      <div ref={containerRef} className="relative flex-1">
        <ReactFlow
          onInit={(instance) => {
            flowRef.current = instance;
          }}
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
          // minZoom is the readability floor (FIT_ZOOM_FLOOR — see above):
          // below it a StepNode's smallest text stops being legible, so
          // instead of shrinking further the graph overflows the container
          // and pan/wheel-zoom take over. It is set on both the root (the
          // invariant clamp that also guards wheel/controls zoom-out) and
          // fitViewOptions (belt-and-braces for the initial fit). maxZoom
          // keeps a two-node graph from being blown up to fill the panel.
          minZoom={FIT_ZOOM_FLOOR}
          fitViewOptions={{ padding: 0.15, maxZoom: 1, minZoom: FIT_ZOOM_FLOOR }}
          proOptions={{ hideAttribution: true }}
          className="bg-surface-base"
        >
          <Background color="var(--edge-subtle)" gap={20} size={1} />
          <Controls
            showInteractive={false}
            className="!bg-surface-raised !border-edge !shadow-none [&>button]:!bg-surface-raised [&>button]:!border-edge [&>button]:!text-content-secondary [&>button:hover]:!bg-surface-overlay [&>button:hover]:!text-content-primary"
          />
          {shouldShowMiniMap(compact, nodes.length) ? (
            <MiniMap
              position="bottom-right"
              pannable={false}
              zoomable={false}
              nodeColor={() => "var(--edge-strong)"}
              maskColor="rgba(0, 0, 0, 0.5)"
              className="!bg-surface-raised !border-edge"
            />
          ) : null}

          {/* Custom SVG markers */}
          <svg>
            <defs>
              <marker id="arrow" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
                <polygon points="0 0, 8 3, 0 6" fill="var(--dag-pending-border)" />
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

      {/* Side Panel — clicking the empty pane deselects, which closes it in
          the read-only embed. In that embed the panel OVERLAYS the canvas
          instead of docking beside it: docking shrinks the flow container the
          moment a node is clicked, which slides the canvas sideways and can
          bury the clicked node under the panel it just opened. */}
      {shouldShowSidePanel(editable, selection.type) && (
        <div
          className={
            editable
              ? "w-80 shrink-0 border-l border-edge bg-surface-overlay overflow-y-auto"
              : "absolute inset-y-0 right-0 z-10 w-80 border-l border-edge bg-surface-overlay overflow-y-auto shadow-card"
          }
        >
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
      )}
    </div>
  );
}
