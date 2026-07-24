import { createContext, useContext, useReducer } from "react";
import type { WorkflowSpec, WorkflowNode, WorkflowEdge, WorkflowNodeKind } from "@/lib/api";
import { emptySpec } from "@/lib/workflow/validation";
import { snapToGrid } from "@/lib/designer/flow";

// ─── State & actions ──────────────────────────────────────────────────────────

export interface WorkflowDraftState {
  spec: WorkflowSpec;
  dirty: boolean;
}

export type WorkflowDraftAction =
  | { type: "reset"; spec: WorkflowSpec }
  | { type: "addNode"; node: WorkflowNode }
  | { type: "removeNode"; nodeId: string }
  | { type: "patchNode"; nodeId: string; patch: Partial<Omit<WorkflowNode, "id">> }
  | { type: "moveNode"; nodeId: string; x: number; y: number }
  | { type: "addEdge"; edge: WorkflowEdge }
  | { type: "removeEdge"; edgeId: string }
  | { type: "patchEdge"; edgeId: string; patch: Partial<Omit<WorkflowEdge, "id">> };

function nextId(prefix: string, existing: string[]): string {
  let i = existing.length + 1;
  while (existing.includes(`${prefix}${i}`)) i++;
  return `${prefix}${i}`;
}

export function workflowDraftReducer(
  state: WorkflowDraftState,
  action: WorkflowDraftAction,
): WorkflowDraftState {
  const { spec } = state;

  switch (action.type) {
    case "reset":
      return { spec: action.spec, dirty: false };

    case "addNode": {
      return {
        spec: { ...spec, nodes: [...spec.nodes, action.node] },
        dirty: true,
      };
    }

    case "removeNode": {
      return {
        spec: {
          ...spec,
          nodes: spec.nodes.filter((n) => n.id !== action.nodeId),
          edges: spec.edges.filter((e) => e.from !== action.nodeId && e.to !== action.nodeId),
        },
        dirty: true,
      };
    }

    case "patchNode": {
      return {
        spec: {
          ...spec,
          nodes: spec.nodes.map((n) => (n.id === action.nodeId ? { ...n, ...action.patch } : n)),
        },
        dirty: true,
      };
    }

    case "moveNode": {
      const x = snapToGrid(action.x);
      const y = snapToGrid(action.y);
      return {
        spec: {
          ...spec,
          nodes: spec.nodes.map((n) => (n.id === action.nodeId ? { ...n, pos: { x, y } } : n)),
        },
        dirty: true,
      };
    }

    case "addEdge": {
      return {
        spec: { ...spec, edges: [...spec.edges, action.edge] },
        dirty: true,
      };
    }

    case "removeEdge": {
      return {
        spec: { ...spec, edges: spec.edges.filter((e) => e.id !== action.edgeId) },
        dirty: true,
      };
    }

    case "patchEdge": {
      const patch = { ...action.patch };
      if (typeof patch.condition === "string" && !patch.condition.trim()) {
        patch.condition = undefined;
      }
      return {
        spec: {
          ...spec,
          edges: spec.edges.map((e) => (e.id === action.edgeId ? { ...e, ...patch } : e)),
        },
        dirty: true,
      };
    }

    default:
      return state;
  }
}

// ─── Context ──────────────────────────────────────────────────────────────────

export interface WorkflowDraftValue {
  state: WorkflowDraftState;
  dispatch: React.Dispatch<WorkflowDraftAction>;
  addNode: (kind: WorkflowNodeKind, x?: number, y?: number) => void;
  removeNode: (nodeId: string) => void;
  patchNode: (nodeId: string, patch: Partial<Omit<WorkflowNode, "id">>) => void;
  moveNode: (nodeId: string, x: number, y: number) => void;
  addEdge: (from: string, to: string, label?: string) => void;
  removeEdge: (edgeId: string) => void;
  patchEdge: (edgeId: string, patch: Partial<Omit<WorkflowEdge, "id">>) => void;
  reset: (spec: WorkflowSpec) => void;
}

const WorkflowDraftContext = createContext<WorkflowDraftValue | null>(null);

export function WorkflowDraftProvider({
  children,
  initialSpec,
}: {
  children: React.ReactNode;
  initialSpec?: WorkflowSpec;
}) {
  const [state, dispatch] = useReducer(workflowDraftReducer, {
    spec: initialSpec ?? emptySpec(),
    dirty: false,
  });

  const addNode = (kind: WorkflowNodeKind, x = 120, y = 120) => {
    const existingIds = state.spec.nodes.map((n) => n.id);
    const id = nextId("n", existingIds);
    const defaultLabel: Record<WorkflowNodeKind, string> = {
      input: "Input",
      chat: "Chat",
      parse: "Parse",
      fanout: "Fan Out",
      engine: "Engine",
    };
    dispatch({
      type: "addNode",
      node: {
        id,
        kind,
        label: defaultLabel[kind] ?? kind,
        pos: { x: snapToGrid(x), y: snapToGrid(y) },
      },
    });
  };

  const removeNode = (nodeId: string) => dispatch({ type: "removeNode", nodeId });

  const patchNode = (nodeId: string, patch: Partial<Omit<WorkflowNode, "id">>) =>
    dispatch({ type: "patchNode", nodeId, patch });

  const moveNode = (nodeId: string, x: number, y: number) =>
    dispatch({ type: "moveNode", nodeId, x, y });

  const addEdge = (from: string, to: string, label?: string) => {
    const existingIds = state.spec.edges.map((e) => e.id);
    const id = nextId("e", existingIds);
    dispatch({ type: "addEdge", edge: { id, from, to, label } });
  };

  const removeEdge = (edgeId: string) => dispatch({ type: "removeEdge", edgeId });

  const patchEdge = (edgeId: string, patch: Partial<Omit<WorkflowEdge, "id">>) =>
    dispatch({ type: "patchEdge", edgeId, patch });

  const reset = (spec: WorkflowSpec) => dispatch({ type: "reset", spec });

  return (
    <WorkflowDraftContext.Provider
      value={{
        state,
        dispatch,
        addNode,
        removeNode,
        patchNode,
        moveNode,
        addEdge,
        removeEdge,
        patchEdge,
        reset,
      }}
    >
      {children}
    </WorkflowDraftContext.Provider>
  );
}

export function useWorkflowDraft(): WorkflowDraftValue {
  const ctx = useContext(WorkflowDraftContext);
  if (!ctx) throw new Error("useWorkflowDraft must be used within WorkflowDraftProvider");
  return ctx;
}
