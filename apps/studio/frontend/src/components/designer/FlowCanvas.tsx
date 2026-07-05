/**
 * FlowCanvas — the causal flow view. Deterministic layout from deriveFlow():
 * operator cards ordered left→right by causal depth, every signal hand-off an
 * edge from emitter to observer. Loops route under their row; long cascades
 * wrap like text lines. Signal identity lives in edge color and the signal
 * index (top right); label chips appear on hover, focus, or close zoom so the
 * resting view stays quiet. Click a signal anywhere to isolate its edges;
 * Escape clears focus, then selection. Pan by dragging the background, zoom
 * with the wheel or the controls.
 *
 * Nodes are draggable: grab a card, drag it anywhere, release to snap to the
 * 16 px grid. Connected edges reroute in real time using frozen channel
 * geometry so layout structure is preserved. Position overrides live in
 * component state and reset when the blueprint changes.
 *
 * Viewport stability is a hard rule: the view transform changes ONLY through
 * user gestures, the fit button, or a blueprint switch. Selection, panels,
 * and container resizes never move the camera — the inspector expands over
 * the selected card as an overlay instead of reflowing the canvas.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import type { FlowEdge, FlowModel, FlowNode } from "@/lib/designer/flow";
import {
  QUIESCENCE,
  SIGNAL_PALETTE,
  SNAP_GRID,
  rerouteEdges,
  snapToGrid,
} from "@/lib/designer/flow";
import EdgeInspector from "./EdgeInspector";
import { IconClose, IconPencil, IconShield, IconTrash } from "@/components/ui/icons";
import { useDesignerDraft } from "./DesignerDraftContext";

const MIN_ZOOM = 0.3;
const MAX_ZOOM = 1.5;
const DETAIL_ZOOM = 0.9; // past this scale label chips show on every edge

// Fit padding. The right gutter clears the signal index panel (208px overlay
// at right-3) so the graph never hides under it; the top clears the header
// strip. The graph is scaled and centered within what's left.
const FIT_PAD = { top: 56, right: 236, bottom: 40, left: 48 };

interface View {
  scale: number;
  tx: number;
  ty: number;
}

function arrowPath(a: FlowEdge["arrow"]): string {
  if (a.dir === "right")
    return `M ${a.x - 7} ${a.y - 4.5} L ${a.x} ${a.y} L ${a.x - 7} ${a.y + 4.5}`;
  return `M ${a.x - 4.5} ${a.y + 7} L ${a.x} ${a.y} L ${a.x + 4.5} ${a.y + 7}`;
}

export interface FlowCanvasControls {
  zoomIn: () => void;
  zoomOut: () => void;
  fit: () => void;
}

export interface FlowCanvasProps {
  model: FlowModel;
  /** Re-fit signal — changes when the topology kind changes. */
  fitKey: string;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  focusSignal: string | null;
  onFocusSignal: (signal: string | null) => void;
  renderNode: (props: { node: FlowNode; x: number; y: number }) => React.ReactNode;
  /** Called once on mount to hand zoom controls to the parent toolbar. */
  onRegisterControls?: (controls: FlowCanvasControls) => void;
  /** When true, the signal index panel is hidden (workflow mode). */
  hideSignalIndex?: boolean;
  /** Called when the user finishes dragging a node (after snap). Workflow mode only. */
  onNodeMoved?: (nodeId: string, x: number, y: number) => void;
  /** Override the default fit padding {top,right,bottom,left}. */
  fitPad?: { top: number; right: number; bottom: number; left: number };
}

export default function FlowCanvas({
  model,
  fitKey,
  selectedId,
  onSelect,
  focusSignal,
  onFocusSignal,
  renderNode,
  onRegisterControls,
  hideSignalIndex,
  onNodeMoved,
  fitPad,
}: FlowCanvasProps) {
  const t = useTranslations("designer.flow");
  const draftCtx = useDesignerDraft();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [view, setView] = useState<View>({ scale: 1, tx: 0, ty: 0 });
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [dragging, setDragging] = useState(false);
  const [hoveredOp, setHoveredOp] = useState<string | null>(null);
  const [hoveredEdge, setHoveredEdge] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [signalFilter, setSignalFilter] = useState("");
  // Signal editing state — which signal is being renamed, and the pending new name/color.
  const [editingSignal, setEditingSignal] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editColor, setEditColor] = useState("");

  // Pan drag ref
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);

  // Node position overrides — keyed by node id, reset on blueprint switch.
  const [posOverrides, setPosOverrides] = useState<Map<string, { x: number; y: number }>>(
    () => new Map(),
  );

  // Per-node drag tracking: nodeId → pointer start and original position.
  const nodeDrag = useRef<{
    nodeId: string;
    pointerId: number;
    startPx: number; // pointer client X at drag start
    startPy: number;
    origX: number; // node canvas position at drag start
    origY: number;
    // Live delta (canvas units) updated on every pointermove
    dx: number;
    dy: number;
    // Stage under the pointer at drag start — pointer capture retargets the
    // click, so a no-movement release selects this stage instead.
    targetStageId: string | null;
  } | null>(null);

  // Live drag translate in canvas units (applied as CSS transform during drag).
  const [liveDelta, setLiveDelta] = useState<{ nodeId: string; dx: number; dy: number } | null>(
    null,
  );

  // One inspect surface at a time: a node selection closes the edge panel,
  // and a blueprint switch invalidates edge ids entirely. Adjusted during
  // render (guarded) so the camera/effects never see the stale selection.
  const [prevFitKey, setPrevFitKey] = useState(fitKey);
  if (prevFitKey !== fitKey) {
    setPrevFitKey(fitKey);
    setSelectedEdgeId(null);
    setPosOverrides(new Map());
  }
  if (selectedId && selectedEdgeId) setSelectedEdgeId(null);

  const selectEdge = useCallback(
    (edgeId: string) => {
      setSelectedEdgeId((cur) => (cur === edgeId ? null : edgeId));
      onSelect(null);
    },
    [onSelect],
  );

  const activePad = fitPad ?? FIT_PAD;

  const fit = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const availW = Math.max(el.clientWidth - activePad.left - activePad.right, 160);
    const availH = Math.max(el.clientHeight - activePad.top - activePad.bottom, 160);
    const scale = Math.min(
      Math.max(Math.min(availW / model.width, availH / model.height), MIN_ZOOM),
      MAX_ZOOM,
    );
    const tx = activePad.left + Math.max((availW - model.width * scale) / 2, 0);
    const ty = activePad.top + Math.max((availH - model.height * scale) / 2, 0);
    setView({ scale, tx, ty });
  }, [model.width, model.height, activePad]);

  useLayoutEffect(() => {
    fit();
    // Refit only when the blueprint identity changes, not on every model tick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey]);

  // Track container size for overlay clamping — never refit on resize: the
  // camera belongs to the user.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Escape backs out one layer at a time: focus, edge panel, then selection.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (focusSignal) onFocusSignal(null);
      else if (selectedEdgeId) setSelectedEdgeId(null);
      else if (selectedId) onSelect(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focusSignal, onFocusSignal, selectedEdgeId, selectedId, onSelect]);

  // Wheel zoom toward the cursor — non-passive so preventDefault works.
  // Scrollable overlays (inspector, signal index) keep their native scroll.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if ((e.target as HTMLElement).closest("[data-flow-wheel]")) return;
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const px = e.clientX - rect.left;
      const py = e.clientY - rect.top;
      setView((v) => {
        const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
        const scale = Math.min(Math.max(v.scale * factor, MIN_ZOOM), MAX_ZOOM);
        if (scale === v.scale) return v;
        const k = scale / v.scale;
        return { scale, tx: px - (px - v.tx) * k, ty: py - (py - v.ty) * k };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  // Canvas pan — only when not on a node or edge hit target.
  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    if ((e.target as HTMLElement).closest("[data-flow-stop]")) return;
    drag.current = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty };
    setDragging(true);
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    // Capture before setView: the updater runs after this handler returns,
    // and endDrag may null the ref in between (crashes on drag.current.tx).
    const d = drag.current;
    if (!d) return;
    const tx = d.tx + (e.clientX - d.x);
    const ty = d.ty + (e.clientY - d.y);
    setView((v) => ({ ...v, tx, ty }));
  };
  const endDrag = (e: React.PointerEvent) => {
    if (drag.current) {
      const moved = Math.abs(e.clientX - drag.current.x) + Math.abs(e.clientY - drag.current.y) > 4;
      drag.current = null;
      setDragging(false);
      if (!moved) {
        if (focusSignal) onFocusSignal(null);
        else if (selectedEdgeId) setSelectedEdgeId(null);
        else onSelect(null);
      }
    }
  };

  // Node drag handlers — attached to the node wrapper via data-node-id.
  // The wrapper lives inside the world transform so pointer coordinates are
  // in screen space; we convert to canvas units via the current view scale.
  const onNodePointerDown = useCallback(
    (e: React.PointerEvent, nodeId: string) => {
      if (e.button !== 0) return;
      e.stopPropagation();
      const node = model.nodes.find((n) => n.id === nodeId);
      if (!node) return;
      const override = posOverrides.get(nodeId);
      const origX = override?.x ?? node.x;
      const origY = override?.y ?? node.y;
      const targetStageId =
        (e.target as HTMLElement).closest("[data-stage-id]")?.getAttribute("data-stage-id") ?? null;
      nodeDrag.current = {
        nodeId,
        pointerId: e.pointerId,
        startPx: e.clientX,
        startPy: e.clientY,
        origX,
        origY,
        dx: 0,
        dy: 0,
        targetStageId,
      };
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      setLiveDelta({ nodeId, dx: 0, dy: 0 });
    },
    [model.nodes, posOverrides],
  );

  const onNodePointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (!nodeDrag.current) return;
      // Convert screen delta to canvas units.
      const dx = (e.clientX - nodeDrag.current.startPx) / view.scale;
      const dy = (e.clientY - nodeDrag.current.startPy) / view.scale;
      nodeDrag.current.dx = dx;
      nodeDrag.current.dy = dy;
      setLiveDelta({ nodeId: nodeDrag.current.nodeId, dx, dy });
    },
    [view.scale],
  );

  const onNodePointerUp = useCallback(() => {
    if (!nodeDrag.current) return;
    const { nodeId, origX, origY, dx, dy, targetStageId } = nodeDrag.current;
    const moved = Math.abs(dx) + Math.abs(dy) > 4;
    nodeDrag.current = null;
    setLiveDelta(null);
    if (!moved) {
      if (targetStageId) onSelect(targetStageId);
      return;
    }
    const snappedX = snapToGrid(origX + dx);
    const snappedY = snapToGrid(origY + dy);
    setPosOverrides((prev) => {
      const next = new Map(prev);
      next.set(nodeId, { x: snappedX, y: snappedY });
      return next;
    });
    onNodeMoved?.(nodeId, snappedX, snappedY);
  }, [onSelect, onNodeMoved]);

  const zoomBy = useCallback(
    (factor: number) =>
      setView((v) => {
        const el = containerRef.current;
        const scale = Math.min(Math.max(v.scale * factor, MIN_ZOOM), MAX_ZOOM);
        if (!el || scale === v.scale) return { ...v, scale };
        const px = el.clientWidth / 2;
        const py = el.clientHeight / 2;
        const k = scale / v.scale;
        return { scale, tx: px - (px - v.tx) * k, ty: py - (py - v.ty) * k };
      }),
    [],
  );

  // Register imperative zoom controls with the parent toolbar once on mount.
  useEffect(() => {
    onRegisterControls?.({
      zoomIn: () => zoomBy(1.2),
      zoomOut: () => zoomBy(1 / 1.2),
      fit,
    });
    // Intentionally omit fit/zoomBy from deps — they are stable callbacks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onRegisterControls]);

  // Focused entity (hover beats selection) — the node whose edges raise.
  const activeNodeId = useMemo(() => {
    const focus = hoveredOp ?? selectedId;
    if (!focus) return null;
    // Members resolve to their group node.
    const node = model.nodes.find((n) => n.id === focus || n.stages.some((s) => s.id === focus));
    return node?.id ?? focus;
  }, [hoveredOp, selectedId, model.nodes]);

  // Participation per node under a signal focus: emits it or observes it.
  const participating = useMemo(() => {
    if (!focusSignal) return null;
    const info = model.signals.find((s) => s.name === focusSignal);
    if (!info) return null;
    const ids = new Set<string>();
    model.nodes.forEach((n) => {
      if (info.observers.includes(n.id)) ids.add(n.id);
      if (n.stages.some((s) => info.emitters.includes(s.id))) ids.add(n.id);
    });
    return ids;
  }, [focusSignal, model.signals, model.nodes]);

  // Rerouted edges: recompute paths whenever position overrides change or a
  // node is being dragged. During drag we add the live delta on top of any
  // committed override so the edges animate with the pointer.
  const displayEdges = useMemo((): FlowEdge[] => {
    const effective = new Map(posOverrides);
    if (liveDelta) {
      const { nodeId, dx, dy } = liveDelta;
      const base = posOverrides.get(nodeId);
      const node = model.nodes.find((n) => n.id === nodeId);
      if (node) {
        const baseX = base?.x ?? node.x;
        const baseY = base?.y ?? node.y;
        effective.set(nodeId, { x: baseX + dx, y: baseY + dy });
      }
    }
    if (effective.size === 0) return model.edges;
    return rerouteEdges(model.edges, model.nodes, effective);
  }, [model.edges, model.nodes, posOverrides, liveDelta]);

  const edgeState = (e: FlowEdge): "dim" | "rest" | "active" => {
    if (focusSignal) return e.signal === focusSignal ? "active" : "dim";
    if (selectedEdgeId === e.id) return "active";
    if (hoveredEdge === e.id) return "active";
    if (activeNodeId && (e.from === activeNodeId || e.to === activeNodeId)) return "active";
    return "rest";
  };

  const showDetail = view.scale >= DETAIL_ZOOM;
  const filteredSignals = model.signals.filter(
    (s) => !signalFilter || s.name.toLowerCase().includes(signalFilter.toLowerCase()),
  );

  // Selected edge + its panel anchor (at the label chip, clamped on screen).
  const selectedEdge = useMemo(
    () => (selectedEdgeId ? (displayEdges.find((e) => e.id === selectedEdgeId) ?? null) : null),
    [selectedEdgeId, displayEdges],
  );
  const edgePanelPos = useMemo(() => {
    if (!selectedEdge?.chip || size.w === 0) return null;
    const sx = selectedEdge.chip.x * view.scale + view.tx;
    const sy = selectedEdge.chip.y * view.scale + view.ty;
    const left = Math.min(Math.max(sx - 132, 8), Math.max(8, size.w - 272));
    const top = Math.min(Math.max(sy + 12, 8), Math.max(8, size.h - 280));
    return { left, top, maxHeight: size.h - top - 8 };
  }, [selectedEdge, size, view]);

  // Resolved node position — committed override or original layout position.
  const nodePos = useCallback((nodeId: string) => posOverrides.get(nodeId), [posOverrides]);

  return (
    <div
      ref={containerRef}
      className="designer-canvas relative h-full w-full overflow-hidden"
      style={{ cursor: dragging ? "grabbing" : "grab", touchAction: "none" }}
      onPointerDown={onPointerDown}
      onPointerMove={(e) => {
        onPointerMove(e);
        onNodePointerMove(e);
      }}
      onPointerUp={(e) => {
        endDrag(e);
        onNodePointerUp();
      }}
      onPointerCancel={(e) => {
        endDrag(e);
        onNodePointerUp();
      }}
    >
      {/* World */}
      <div
        className="absolute left-0 top-0"
        style={{
          width: model.width,
          height: model.height,
          transform: `translate(${view.tx}px, ${view.ty}px) scale(${view.scale})`,
          transformOrigin: "0 0",
        }}
      >
        {/* Vignette glow — world-anchored so it never moves on container resize */}
        <div
          aria-hidden="true"
          className="designer-vignette absolute"
          style={{
            left: model.width / 2 - model.width * 0.85,
            top: model.height / 2 - model.width * 0.62,
            width: model.width * 1.7,
            height: model.width * 1.24,
          }}
        />
        {/* Edges */}
        <svg
          width={model.width}
          height={model.height}
          className="absolute left-0 top-0"
          aria-hidden="true"
        >
          {displayEdges.map((e) => {
            const state = edgeState(e);
            const neutral = !e.signal || e.signal === QUIESCENCE;
            // Self-loops frame the card; the spawn rule + port labels already
            // tell the story, so they stay faint until hovered.
            const opacity =
              state === "dim"
                ? 0.06
                : state === "active"
                  ? 0.95
                  : e.kind === "self"
                    ? 0.28
                    : neutral
                      ? 0.4
                      : 0.6;
            const width = state === "active" ? 2 : 1.5;
            const dash =
              e.kind === "quiescence"
                ? "2 5"
                : e.kind === "loop" || e.kind === "self"
                  ? "6 4"
                  : undefined;
            return (
              <g key={e.id}>
                <path
                  d={e.path}
                  fill="none"
                  stroke={e.color}
                  strokeWidth={width}
                  strokeLinecap="round"
                  strokeDasharray={dash}
                  opacity={opacity}
                />
                <path
                  d={arrowPath(e.arrow)}
                  fill="none"
                  stroke={e.color}
                  strokeWidth={width}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  opacity={opacity}
                />
                {/* Invisible hit area — click opens the edge inspector */}
                <path
                  d={e.path}
                  data-flow-stop
                  fill="none"
                  stroke="transparent"
                  strokeWidth={14}
                  style={{ cursor: "pointer" }}
                  onPointerEnter={() => setHoveredEdge(e.id)}
                  onPointerLeave={() => setHoveredEdge((h) => (h === e.id ? null : h))}
                  onClick={(ev) => {
                    ev.stopPropagation();
                    selectEdge(e.id);
                  }}
                />
              </g>
            );
          })}
        </svg>

        {/* Edge label chips. Ports already name the signal at both endpoints,
            so at rest a chip appears only where it adds information: loop/wrap
            mid-channel identity, or a gate condition on a forward edge. Self
            and quiescence edges stay chipless until hovered. */}
        {displayEdges.map((e) => {
          const state = edgeState(e);
          if (state === "dim") return null;
          if (state !== "active" && !showDetail) return null;
          const isLoop = e.kind === "loop" || e.kind === "self";
          const hoverLabel =
            e.kind === "quiescence" ? t("quiescence") : (e.signal ?? e.condition ?? null);
          const restLabel =
            e.kind === "loop" || e.kind === "wrap"
              ? (e.signal ?? e.condition ?? null)
              : e.kind === "forward"
                ? (e.condition ?? (e.judgeGated ? e.signal : null) ?? null)
                : null;
          const label = state === "active" ? hoverLabel : restLabel;
          if (!label) return null;
          const isSignalChip = Boolean(e.signal) && e.kind !== "quiescence";
          return (
            <div
              key={`chip-${e.id}`}
              data-flow-stop
              className="absolute -translate-x-1/2 -translate-y-1/2"
              style={{ left: e.chip.x, top: e.chip.y }}
            >
              <button
                type="button"
                tabIndex={-1}
                onClick={(ev) => {
                  ev.stopPropagation();
                  selectEdge(e.id);
                }}
                onPointerEnter={() => setHoveredEdge(e.id)}
                onPointerLeave={() => setHoveredEdge((h) => (h === e.id ? null : h))}
                className={`flex cursor-pointer items-center gap-1.5 overflow-hidden whitespace-nowrap rounded-full border bg-surface-raised px-2 py-px font-data text-[length:var(--t-xs)] ${
                  state === "active"
                    ? "max-w-[200px] border-edge-strong shadow-sm"
                    : "max-w-[116px] border-edge"
                }`}
                style={{ opacity: state === "active" ? 1 : 0.92 }}
                title={[e.signal, e.condition, e.bound].filter(Boolean).join(" · ")}
              >
                {isSignalChip && (
                  <span
                    aria-hidden="true"
                    className="h-1.5 w-1.5 shrink-0 rounded-full"
                    style={{ background: e.color }}
                  />
                )}
                {e.judgeGated && (
                  <span className="shrink-0 text-accent" title="judge-gated">
                    <IconShield size={9} />
                  </span>
                )}
                <span
                  className={`truncate ${isSignalChip ? "text-content-secondary" : "text-content-muted"}`}
                >
                  {isLoop ? `↺ ${label}` : label}
                </span>
              </button>
            </div>
          );
        })}

        {/* Node cards — each wrapper captures drag events and feeds the
            resolved world position into renderNode. */}
        {model.nodes.map((n) => {
          const dimmed = participating ? !participating.has(n.id) : false;
          const ov = nodePos(n.id);
          const isDragging = liveDelta?.nodeId === n.id;
          const ldx = isDragging ? liveDelta!.dx : 0;
          const ldy = isDragging ? liveDelta!.dy : 0;
          return (
            <div
              key={n.id}
              data-flow-stop
              className="transition-opacity duration-150"
              style={{
                opacity: dimmed ? 0.35 : 1,
                cursor: isDragging ? "grabbing" : "grab",
              }}
              onPointerDown={(e) => onNodePointerDown(e, n.id)}
              onPointerEnter={() => setHoveredOp(n.id)}
              onPointerLeave={() => setHoveredOp((h) => (h === n.id ? null : h))}
            >
              {renderNode({ node: n, x: (ov?.x ?? n.x) + ldx, y: (ov?.y ?? n.y) + ldy })}
            </div>
          );
        })}
      </div>

      {/* Edge inspector — the full story of one hand-off */}
      {selectedEdge && edgePanelPos && (
        <div
          key={selectedEdge.id}
          data-flow-stop
          data-flow-wheel
          className="inspector-in absolute z-20 flex"
          style={{
            left: edgePanelPos.left,
            top: edgePanelPos.top,
            maxHeight: edgePanelPos.maxHeight,
          }}
        >
          <EdgeInspector
            edge={selectedEdge}
            model={model}
            onTrace={(sig) => {
              onFocusSignal(sig);
              setSelectedEdgeId(null);
            }}
            onClose={() => setSelectedEdgeId(null)}
            onDelete={
              draftCtx
                ? () => {
                    const { from, to, signal } = selectedEdge;
                    draftCtx.patchTopo((topo) => ({
                      ...topo,
                      edges: topo.edges.filter(
                        (e) => !(e.from === from && e.to === to && (e.on ?? undefined) === signal),
                      ),
                    }));
                    setSelectedEdgeId(null);
                  }
                : undefined
            }
          />
        </div>
      )}

      {/* Signal index — identity, fan-in/out, focus. Editable when draftCtx
          is available (designer mode). Hidden while a stage is selected or in workflow mode. */}
      <div
        data-flow-stop
        data-flow-wheel
        className={`absolute right-3 top-3 flex w-[216px] flex-col overflow-hidden rounded-md border border-edge bg-surface-raised shadow-sm ${
          selectedId || hideSignalIndex ? "hidden" : ""
        }`}
      >
        <div className="flex items-center justify-between border-b border-edge px-2.5 py-1.5">
          <span className="font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.11em] text-content-muted">
            {t("signals")}
          </span>
          {focusSignal && (
            <button
              type="button"
              onClick={() => onFocusSignal(null)}
              className="flex items-center gap-1 rounded px-1 font-data text-[length:var(--t-xs)] text-content-muted hover:text-content-primary"
            >
              {t("showAll")}
              <IconClose size={8} strokeWidth={2.5} />
            </button>
          )}
        </div>
        {model.signals.length > 8 && (
          <input
            type="text"
            value={signalFilter}
            onChange={(e) => setSignalFilter(e.target.value)}
            placeholder={t("filter")}
            className="border-b border-edge bg-transparent px-2.5 py-1 font-data text-[length:var(--t-xs)] text-content-primary placeholder:text-content-muted focus:outline-none"
          />
        )}
        <div className="max-h-[38vh] overflow-y-auto py-1">
          {filteredSignals.map((s) => {
            const focused = focusSignal === s.name;
            const isEditing = editingSignal === s.name;
            return (
              <div key={s.name} className="group relative">
                {isEditing && draftCtx ? (
                  // Inline edit form — rename + recolor
                  <div className="flex flex-col gap-1.5 px-2.5 py-2">
                    <input
                      type="text"
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                      className="h-6 rounded border border-edge bg-surface-overlay px-2 font-data text-[length:var(--t-xs)] text-content-primary focus:outline-none focus:ring-1 focus:ring-accent"
                    />
                    <div className="flex flex-wrap gap-1">
                      {SIGNAL_PALETTE.map((c) => (
                        <button
                          key={c}
                          type="button"
                          aria-label={`color ${c}`}
                          onClick={() => setEditColor(c)}
                          className="h-4 w-4 rounded-full border-2 transition-transform hover:scale-110"
                          style={{
                            background: c,
                            borderColor: editColor === c ? "var(--content-primary)" : "transparent",
                          }}
                        />
                      ))}
                    </div>
                    <div className="flex gap-1.5">
                      <button
                        type="button"
                        onClick={() => {
                          const newName = editName.trim();
                          if (newName && newName !== s.name) {
                            draftCtx.patchTopo((topo) => ({
                              ...topo,
                              stages: topo.stages.map((st) => ({
                                ...st,
                                emits: st.emits.map((e) => (e === s.name ? newName : e)),
                              })),
                              edges: topo.edges.map((e) =>
                                e.on === s.name ? { ...e, on: newName } : e,
                              ),
                            }));
                            if (focusSignal === s.name) onFocusSignal(newName);
                          }
                          setEditingSignal(null);
                        }}
                        className="flex h-6 flex-1 items-center justify-center rounded bg-accent font-ui text-[length:var(--t-xs)] text-white hover:opacity-90"
                      >
                        Save
                      </button>
                      <button
                        type="button"
                        onClick={() => setEditingSignal(null)}
                        className="flex h-6 items-center justify-center rounded border border-edge px-1.5 text-content-muted hover:text-content-primary"
                      >
                        <IconClose size={9} strokeWidth={2} />
                      </button>
                    </div>
                  </div>
                ) : (
                  <button
                    type="button"
                    onClick={() => onFocusSignal(focused ? null : s.name)}
                    className={`flex w-full items-center gap-2 px-2.5 py-1 text-left hover:bg-surface-overlay ${
                      focused ? "bg-surface-overlay" : ""
                    }`}
                    title={`${s.emitters.length} → · → ${s.observers.length}`}
                  >
                    <span
                      aria-hidden="true"
                      className={`h-2 w-2 shrink-0 rounded-full ${s.system ? "border border-current bg-transparent" : ""}`}
                      style={s.system ? { color: s.color } : { background: s.color }}
                    />
                    <span
                      className={`min-w-0 flex-1 truncate font-data text-[length:var(--t-xs)] ${
                        focused ? "text-content-primary" : "text-content-secondary"
                      }`}
                    >
                      {s.system ? t("quiescence") : s.name}
                    </span>
                    <span className="shrink-0 font-data text-[length:var(--t-xs)] tabular-nums text-content-muted">
                      {s.emitters.length}→{s.observers.length}
                    </span>
                  </button>
                )}
                {/* Edit / delete actions — appear on hover, only in designer mode */}
                {!isEditing && !s.system && draftCtx && (
                  <div className="absolute right-1 top-1/2 hidden -translate-y-1/2 items-center gap-0.5 group-hover:flex">
                    <button
                      type="button"
                      aria-label={`Edit ${s.name}`}
                      title="Edit signal"
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditingSignal(s.name);
                        setEditName(s.name);
                        setEditColor(s.color);
                      }}
                      className="flex h-5 w-5 items-center justify-center rounded text-content-muted hover:bg-surface-overlay hover:text-content-primary"
                    >
                      <IconPencil size={9} strokeWidth={1.75} />
                    </button>
                    <button
                      type="button"
                      aria-label={`Delete ${s.name}`}
                      title="Delete signal"
                      onClick={(e) => {
                        e.stopPropagation();
                        const hasEdges = draftCtx.topo.edges.some((ed) => ed.on === s.name);
                        const msg = hasEdges
                          ? `Remove signal "${s.name}" and its ${draftCtx.topo.edges.filter((ed) => ed.on === s.name).length} edge(s)?`
                          : `Remove signal "${s.name}"?`;
                        if (!confirm(msg)) return;
                        draftCtx.patchTopo((topo) => ({
                          ...topo,
                          stages: topo.stages.map((st) => ({
                            ...st,
                            emits: st.emits.filter((em) => em !== s.name),
                          })),
                          edges: topo.edges.filter((ed) => ed.on !== s.name),
                        }));
                        if (focusSignal === s.name) onFocusSignal(null);
                      }}
                      className="flex h-5 w-5 items-center justify-center rounded text-content-muted hover:bg-surface-overlay hover:text-status-danger"
                    >
                      <IconTrash size={9} strokeWidth={1.75} />
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Snap grid hint — shows while a node drag is live */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute bottom-14 left-1/2 -translate-x-1/2"
        style={{ opacity: liveDelta ? 0.55 : 0, transition: "opacity 0.15s" }}
      >
        <span className="rounded bg-surface-raised px-2 py-0.5 font-data text-[length:var(--t-xs)] text-content-muted shadow">
          {SNAP_GRID}px grid
        </span>
      </div>
    </div>
  );
}
