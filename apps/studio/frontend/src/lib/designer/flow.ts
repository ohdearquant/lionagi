/**
 * Flow derivation — projects an EngineTopology onto a port-based node graph:
 * every operator is a card whose LEFT edge carries input ports (what it
 * observes) and whose RIGHT edge carries output ports (what it emits). Edges
 * connect an output port to an input port, so a hand-off always reads
 * left→right at the port level even when the edge loops back.
 *
 * The hand-off itself is an event: a stage finishing produces its final
 * response message, rendered as the neutral `handoff` port. The seq spine of
 * an engine IS the handoff chain; reaction/loop edges ride named emission
 * classes with signal-identity color.
 *
 * Spawn-unit groups render as ONE composite card: member stages are rows
 * inside the card, ports live on the group boundary (matching the runtime —
 * reactions observe at the unit boundary, not a member stage).
 *
 * Semantic ground truth (lionagi/engines/engine.py): `run.observe(Type, …)`
 * subscribes to an emission TYPE — source-agnostic. The flow edge therefore
 * pairs each actual emitter of a type with each observer of that type; the
 * signal index keeps the type-level view ("who else is on this signal").
 */
import type { EngineTopology, TopologyStage } from "./topology";

// ─── Snap grid — matches the canvas dot grid ─────────────────────────────────

export const SNAP_GRID = 16;

export function snapToGrid(v: number): number {
  return Math.round(v / SNAP_GRID) * SNAP_GRID;
}

// ─── Geometry constants ───────────────────────────────────────────────────────

export const CARD_W = 224;
export const INPUT_W = 176;

export const HEADER_H = 26; // identity row
export const BINDING_H = 20; // role · model row
export const RULE_H = 16; // group spawn-rule line
export const MEMBER_H = 22; // one member row inside a composite card
export const PORT_H = 18; // one port row
const PORTS_TOP_PAD = 3;
const PAD_BOTTOM = 5;

const LAYER_GAP = 120; // edge-label room between columns (gate-condition chips)
const V_GAP = 28; // between stacked entities in one column
const MARGIN_X = 40;
const MARGIN_TOP = 56; // header-strip clearance
const MARGIN_BOTTOM = 44; // toolbar clearance

const MAX_LAYERS_PER_ROW = 6; // serpentine wrap past this — keeps zoom readable
const CHANNEL_BASE = 30; // inter-row channel before lanes
const LANE_STEP = 16; // per routed edge through a channel
const SELF_BASE = 18; // first self-loop drop below the card
const SELF_STEP = 12; // per additional self-loop
const EXIT_PAD = 18; // horizontal clearance when leaving/entering for loops
const WRAP_MARGIN = 28; // wrap channel clearance right of the row

export const QUIESCENCE = "quiescence";
export const HANDOFF = "handoff";

/** Signal identity palette — color = signal class, assigned in first-appearance order. */
export const SIGNAL_PALETTE = [
  "#22d3ee", // cyan
  "#3b82f6", // blue
  "#e8a33d", // amber
  "#4fb477", // green
  "#a78bfa", // violet
  "#f472b6", // pink
  "#2dd4bf", // teal
  "#e5604c", // red
];

// ─── Model types ──────────────────────────────────────────────────────────────

export interface PortSpec {
  /** Signal class name, HANDOFF, or QUIESCENCE. */
  name: string;
  side: "in" | "out";
  /** Offset from the node top to the port row center. */
  relY: number;
  color: string;
  /** handoff / quiescence — structural, rendered neutral. */
  system?: boolean;
}

export interface MemberRow {
  stage: TopologyStage;
  /** Offset from the node top to the row top. */
  relY: number;
}

export interface FlowNode {
  kind: "op" | "group";
  id: string;
  stages: TopologyStage[];
  /** Group label (groups only). */
  label?: string;
  /** Mono spec line: which signals spawn a new unit of this group. */
  spawnRule?: string | null;
  /** Node-type tag rendered in the card header (entry/agent/team/…). */
  typeLabel: string;
  x: number;
  y: number;
  w: number;
  h: number;
  layer: number;
  row: number;
  inPorts: PortSpec[];
  outPorts: PortSpec[];
  /** Member rows (groups only, in sequential order). */
  members?: MemberRow[];
}

export type FlowEdgeKind = "forward" | "wrap" | "loop" | "self" | "quiescence";

export interface FlowEdge {
  id: string;
  from: string;
  to: string;
  kind: FlowEdgeKind;
  /** Emission class carried by this hand-off; undefined = handoff (final response). */
  signal?: string;
  color: string;
  condition?: string;
  bound?: string;
  judgeGated?: boolean;
  path: string;
  /** Label anchor (chip centers on x). */
  chip: { x: number; y: number };
  /** Arrowhead position and direction at the target. */
  arrow: { x: number; y: number; dir: "right" | "up" };
  /** Routing constants frozen at layout time — used for live rerouting on drag. */
  _routing?: {
    fromRelY: number;
    toRelY: number;
    channelY?: number;
    selfIdx?: number;
    wrapX?: number;
    wrapLeftX?: number;
    skip?: number;
  };
}

export interface SignalInfo {
  name: string;
  color: string;
  /** Stage ids that emit this class (incl. implied re-posts). */
  emitters: string[];
  /** Entity ids subscribed to this class. */
  observers: string[];
  system?: boolean;
}

export interface ObserveSpec {
  signal: string;
  condition?: string;
  bound?: string;
  judgeGated?: boolean;
}

export interface FlowModel {
  nodes: FlowNode[];
  edges: FlowEdge[];
  signals: SignalInfo[];
  width: number;
  height: number;
  /** opId → mono spawn-rule line (the operations-layer bridge). */
  spawnRules: Record<string, string>;
  /** opId/groupId → subscriptions (for the operator panel). */
  observes: Record<string, ObserveSpec[]>;
  /** signal → identity color (quiescence included when used). */
  signalColor: Record<string, string>;
}

// ─── Path helpers ─────────────────────────────────────────────────────────────

/** Rounded orthogonal polyline through the given points. */
function ortho(pts: Array<[number, number]>, r = 6): string {
  let d = `M ${pts[0][0]} ${pts[0][1]}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [x0, y0] = pts[i - 1];
    const [x1, y1] = pts[i];
    const [x2, y2] = pts[i + 1];
    const inLen = Math.abs(x1 - x0) + Math.abs(y1 - y0);
    const outLen = Math.abs(x2 - x1) + Math.abs(y2 - y1);
    const rr = Math.min(r, inLen / 2, outLen / 2);
    const inDx = Math.sign(x1 - x0);
    const inDy = Math.sign(y1 - y0);
    const outDx = Math.sign(x2 - x1);
    const outDy = Math.sign(y2 - y1);
    d += ` L ${x1 - inDx * rr} ${y1 - inDy * rr} Q ${x1} ${y1} ${x1 + outDx * rr} ${y1 + outDy * rr}`;
  }
  const [lx, ly] = pts[pts.length - 1];
  d += ` L ${lx} ${ly}`;
  return d;
}

function bezier(x1: number, y1: number, x2: number, y2: number, bow = 0): string {
  const dx = Math.min(80, Math.max(32, (x2 - x1) / 2));
  return `M ${x1} ${y1} C ${x1 + dx} ${y1 + bow}, ${x2 - dx} ${y2 + bow}, ${x2 - 5} ${y2}`;
}

interface NodePos {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Compute path/chip/arrow for one edge from current node positions and the
 *  frozen routing constants. Shared by initial layout and live drag reroute. */
function routeEdge(e: FlowEdge, a: NodePos, b: NodePos): Pick<FlowEdge, "path" | "chip" | "arrow"> {
  const r = e._routing ?? { fromRelY: a.h / 2, toRelY: b.h / 2 };
  const y1 = a.y + r.fromRelY;
  const y2 = b.y + r.toRelY;

  if (e.kind === "self") {
    const idx = r.selfIdx ?? 0;
    const loopY = a.y + a.h + SELF_BASE + idx * SELF_STEP;
    const exitX = a.x + a.w + EXIT_PAD - 4;
    const enterX = a.x - (EXIT_PAD - 4);
    return {
      path: ortho([
        [a.x + a.w, y1],
        [exitX, y1],
        [exitX, loopY],
        [enterX, loopY],
        [enterX, y2],
        [a.x - 4, y2],
      ]),
      chip: { x: a.x + a.w / 2, y: loopY },
      arrow: { x: a.x, y: y2, dir: "right" },
    };
  }

  if (e.kind === "loop") {
    const laneY = Math.max(r.channelY ?? 0, a.y + a.h + 12, b.y + b.h + 12);
    const exitX = a.x + a.w + EXIT_PAD;
    const enterX = b.x - EXIT_PAD;
    return {
      path: ortho([
        [a.x + a.w, y1],
        [exitX, y1],
        [exitX, laneY],
        [enterX, laneY],
        [enterX, y2],
        [b.x - 4, y2],
      ]),
      chip: { x: (exitX + enterX) / 2, y: laneY },
      arrow: { x: b.x, y: y2, dir: "right" },
    };
  }

  if (e.kind === "wrap" && r.wrapX !== undefined && r.wrapLeftX !== undefined) {
    const laneY = r.channelY ?? Math.max(a.y + a.h, b.y + b.h) + 24;
    return {
      path: ortho([
        [a.x + a.w, y1],
        [r.wrapX, y1],
        [r.wrapX, laneY],
        [r.wrapLeftX, laneY],
        [r.wrapLeftX, y2],
        [b.x - 4, y2],
      ]),
      chip: { x: (r.wrapX + r.wrapLeftX) / 2, y: laneY },
      arrow: { x: b.x, y: y2, dir: "right" },
    };
  }

  // forward / quiescence — port-to-port bezier.
  const x1 = a.x + a.w;
  const x2 = b.x;
  if (x2 - x1 < 20) {
    // Dragged into overlap/reversal — route below both cards as a U.
    const underY = Math.max(a.y + a.h, b.y + b.h) + 28;
    return {
      path: ortho([
        [x1, y1],
        [x1 + EXIT_PAD, y1],
        [x1 + EXIT_PAD, underY],
        [x2 - EXIT_PAD, underY],
        [x2 - EXIT_PAD, y2],
        [x2 - 4, y2],
      ]),
      chip: { x: (x1 + x2) / 2, y: underY },
      arrow: { x: x2, y: y2, dir: "right" },
    };
  }
  const skip = r.skip ?? 0;
  const bow = skip > 0 ? -(24 + 14 * skip) : 0;
  return {
    path: bezier(x1, y1, x2, y2, bow),
    chip: { x: (x1 + x2) / 2, y: (y1 + y2) / 2 + bow * 0.6 - 9 },
    arrow: { x: x2, y: y2, dir: "right" },
  };
}

// ─── Derivation ───────────────────────────────────────────────────────────────

function shortCond(cond: string | undefined, max = 36): string | undefined {
  if (!cond) return undefined;
  return cond.length > max ? `${cond.slice(0, max - 1)}…` : cond;
}

const KIND_TYPE: Record<TopologyStage["kind"], string> = {
  input: "entry",
  agent: "agent",
  team: "team",
  tool: "tool",
  synth: "synth",
};

export function deriveFlow(topo: EngineTopology): FlowModel {
  const stages = topo.stages;
  const stageById = new Map(stages.map((s) => [s.id, s]));
  const groupOf = (id: string) => stageById.get(id)?.group;

  // ── Entities — group-collapsed units ─────────────────────────────────────
  interface Entity {
    kind: "op" | "group";
    id: string;
    stages: TopologyStage[];
  }
  const entities: Entity[] = [];
  const entityById = new Map<string, Entity>();
  stages.forEach((s) => {
    if (s.group) {
      let g = entityById.get(s.group);
      if (!g) {
        g = { kind: "group", id: s.group, stages: [] };
        entities.push(g);
        entityById.set(s.group, g);
      }
      g.stages.push(s);
    } else {
      const e: Entity = { kind: "op", id: s.id, stages: [s] };
      entities.push(e);
      entityById.set(s.id, e);
    }
  });
  const entityOf = (stageId: string): Entity => {
    const g = groupOf(stageId);
    return entityById.get(g ?? stageId)!;
  };

  // ── Entity-level edges, classified ───────────────────────────────────────
  interface RawEdge {
    from: string;
    to: string;
    kind: FlowEdgeKind;
    signal?: string;
    condition?: string;
    bound?: string;
    judgeGated?: boolean;
  }
  const rawEdges: RawEdge[] = [];
  topo.edges.forEach((e) => {
    const from = entityOf(e.from).id;
    const to = entityOf(e.to).id;
    const isQuiescence = e.kind === "seq" && Boolean(e.condition?.includes("quiescence"));
    if (from === to && !isQuiescence) {
      // Inner seq inside a group orders the members; loops re-entering the
      // unit render as a self edge on the composite card.
      if (e.kind === "seq") return;
      rawEdges.push({
        from,
        to,
        kind: "self",
        signal: e.on,
        condition: e.condition,
        bound: e.bound,
        judgeGated: e.judgeGated,
      });
      return;
    }
    rawEdges.push({
      from,
      to,
      kind: isQuiescence ? "quiescence" : e.kind === "loop" ? "loop" : "forward",
      signal: e.on,
      condition: isQuiescence ? undefined : e.condition,
      bound: e.bound,
      judgeGated: e.judgeGated,
    });
  });

  // ── Layering — longest path over forward + quiescence edges ─────────────
  // The seq spine is the handoff chain; reactions extend it.
  const layerOf = new Map<string, number>();
  entities.forEach((e) => layerOf.set(e.id, 0));
  const ordering = rawEdges.filter((e) => e.kind === "forward" || e.kind === "quiescence");
  for (let pass = 0; pass < entities.length + 1; pass++) {
    let changed = false;
    ordering.forEach((e) => {
      const next = layerOf.get(e.from)! + 1;
      if (next > layerOf.get(e.to)!) {
        layerOf.set(e.to, next);
        changed = true;
      }
    });
    if (!changed) break;
  }
  rawEdges.forEach((e) => {
    if (e.kind === "forward" && layerOf.get(e.to)! <= layerOf.get(e.from)!) e.kind = "loop";
  });

  const layerCount = Math.max(...[...layerOf.values()]) + 1;

  // ── Rows — serpentine wrap so long cascades stay zoom-readable ───────────
  const rowCount = Math.ceil(layerCount / MAX_LAYERS_PER_ROW);
  const layersPerRow = Math.ceil(layerCount / rowCount);
  const rowOfLayer = (layer: number) => Math.floor(layer / layersPerRow);
  const colOfLayer = (layer: number) => layer % layersPerRow;

  // ── Signals — first-appearance order: declared emits, then triggers ─────
  const signalOrder: string[] = [];
  const pushSignal = (s: string) => {
    if (!signalOrder.includes(s)) signalOrder.push(s);
  };
  stages.forEach((st) => st.emits.forEach(pushSignal));
  topo.edges.forEach((e) => {
    if (e.on) pushSignal(e.on);
  });
  const usesQuiescence = topo.edges.some((e) => e.condition?.includes("quiescence"));

  const signalColor: Record<string, string> = {};
  signalOrder.forEach((s, i) => {
    signalColor[s] = SIGNAL_PALETTE[i % SIGNAL_PALETTE.length];
  });
  if (usesQuiescence) signalColor[QUIESCENCE] = "var(--content-muted)";
  const colorOf = (sig: string | undefined) =>
    sig && sig !== QUIESCENCE ? (signalColor[sig] ?? "var(--edge-strong)") : "var(--edge-strong)";

  // ── Ports per entity — inputs (observed) left, outputs (emitted) right ──
  const portNameOf = (e: RawEdge): string =>
    e.kind === "quiescence" ? QUIESCENCE : (e.signal ?? HANDOFF);

  const inNames = new Map<string, string[]>();
  const outNames = new Map<string, string[]>();
  entities.forEach((e) => {
    inNames.set(e.id, []);
    outNames.set(e.id, []);
  });
  const pushName = (m: Map<string, string[]>, id: string, name: string) => {
    const list = m.get(id)!;
    if (!list.includes(name)) list.push(name);
  };
  // Handoff ports first so the seq spine rides the top of each card.
  rawEdges.forEach((e) => {
    const name = portNameOf(e);
    if (name === HANDOFF) {
      pushName(outNames, e.from, HANDOFF);
      pushName(inNames, e.to, HANDOFF);
    }
  });
  // Declared emissions in source order, even unconsumed ones (they land in
  // the emission store).
  entities.forEach((e) => {
    e.stages.forEach((s) => s.emits.forEach((sig) => pushName(outNames, e.id, sig)));
  });
  rawEdges.forEach((e) => {
    const name = portNameOf(e);
    if (name === HANDOFF) return;
    pushName(outNames, e.from, name);
    pushName(inNames, e.to, name);
  });

  const portColor = (name: string) =>
    name === HANDOFF || name === QUIESCENCE ? "var(--edge-strong)" : colorOf(name);

  // ── Entity sizing — header + binding/members + stacked port rows ────────
  const selfCount = new Map<string, number>();
  rawEdges.forEach((e) => {
    if (e.kind === "self") selfCount.set(e.from, (selfCount.get(e.from) ?? 0) + 1);
  });

  interface Sized {
    w: number;
    h: number;
    portsTop: number;
    membersTop: number;
  }
  const sizeOf = (e: Entity): Sized => {
    const nPorts = inNames.get(e.id)!.length + outNames.get(e.id)!.length;
    const portsH = nPorts > 0 ? PORTS_TOP_PAD + nPorts * PORT_H : 0;
    if (e.kind === "group") {
      const membersTop = HEADER_H + RULE_H;
      const h = membersTop + e.stages.length * MEMBER_H + portsH + PAD_BOTTOM;
      return { w: CARD_W, h, portsTop: membersTop + e.stages.length * MEMBER_H, membersTop };
    }
    const s = e.stages[0];
    if (s.kind === "input") {
      return { w: INPUT_W, h: HEADER_H + portsH + PAD_BOTTOM, portsTop: HEADER_H, membersTop: 0 };
    }
    const bindingH = s.role || s.modelStage != null ? BINDING_H : 0;
    return {
      w: CARD_W,
      h: HEADER_H + bindingH + portsH + PAD_BOTTOM,
      portsTop: HEADER_H + bindingH,
      membersTop: 0,
    };
  };

  // ── Vertical order within a column — barycenter of predecessors ─────────
  const preds = new Map<string, string[]>();
  ordering.forEach((e) => {
    (preds.get(e.to) ?? preds.set(e.to, []).get(e.to)!).push(e.from);
  });
  const byLayer = new Map<number, Entity[]>();
  entities.forEach((e) => {
    const l = layerOf.get(e.id)!;
    (byLayer.get(l) ?? byLayer.set(l, []).get(l)!).push(e);
  });

  // Self loops hang below a card — reserve their depth when stacking.
  const stackExtra = (e: Entity) => {
    const n = selfCount.get(e.id) ?? 0;
    return n > 0 ? SELF_BASE + n * SELF_STEP : 0;
  };
  const stackHeight = (list: Entity[]) =>
    list.reduce((sum, e, i) => sum + sizeOf(e).h + stackExtra(e) + (i > 0 ? V_GAP : 0), 0);

  const rowHeights: number[] = [];
  for (let r = 0; r < rowCount; r++) {
    let h = 0;
    for (let l = r * layersPerRow; l < Math.min(layerCount, (r + 1) * layersPerRow); l++) {
      h = Math.max(h, stackHeight(byLayer.get(l) ?? []));
    }
    rowHeights.push(Math.max(h, 64));
  }

  // ── Channel allocation — loop + wrap edges run between rows ─────────────
  interface ChannelEdge {
    edge: RawEdge;
    channel: number;
    lane: number;
  }
  const channelEdges: ChannelEdge[] = [];
  const channelLaneCount: number[] = Array.from({ length: rowCount }, () => 0);
  rawEdges.forEach((e) => {
    const fr = rowOfLayer(layerOf.get(e.from)!);
    const tr = rowOfLayer(layerOf.get(e.to)!);
    if ((e.kind === "forward" || e.kind === "quiescence") && tr !== fr) e.kind = "wrap";
    if (e.kind === "wrap" || e.kind === "loop") {
      const ch = e.kind === "loop" ? Math.max(fr, tr) : fr;
      channelEdges.push({ edge: e, channel: ch, lane: channelLaneCount[ch]++ });
    }
  });

  const channelHeight = (r: number) =>
    channelLaneCount[r] > 0 ? CHANNEL_BASE + channelLaneCount[r] * LANE_STEP : V_GAP + 8;

  // ── Coordinates ──────────────────────────────────────────────────────────
  const rowY: number[] = [];
  let yCursor = MARGIN_TOP;
  for (let r = 0; r < rowCount; r++) {
    rowY.push(yCursor);
    yCursor += rowHeights[r] + (r < rowCount - 1 ? channelHeight(r) : 0);
  }

  const nodes: FlowNode[] = [];
  const nodeById = new Map<string, FlowNode>();
  for (let l = 0; l < layerCount; l++) {
    const list = byLayer.get(l) ?? [];
    const indexIn = (id: string) => {
      const ll = layerOf.get(id)!;
      return (byLayer.get(ll) ?? []).findIndex((e) => e.id === id);
    };
    list.sort((a, b) => {
      const bary = (e: Entity) => {
        const ps = preds.get(e.id) ?? [];
        if (ps.length === 0) return 0;
        return ps.reduce((s, p) => s + indexIn(p), 0) / ps.length;
      };
      return bary(a) - bary(b);
    });

    const r = rowOfLayer(l);
    const col = colOfLayer(l);
    const colX = MARGIN_X + col * (CARD_W + LAYER_GAP);
    const stackH = stackHeight(list);
    let y = rowY[r] + Math.max(0, (rowHeights[r] - stackH) / 2);
    list.forEach((e) => {
      const sized = sizeOf(e);
      const x = colX + (CARD_W - sized.w) / 2;
      const ins = inNames.get(e.id)!;
      const outs = outNames.get(e.id)!;
      const portY = (idx: number) => sized.portsTop + PORTS_TOP_PAD + idx * PORT_H + PORT_H / 2;
      const inPorts: PortSpec[] = ins.map((name, i) => ({
        name,
        side: "in" as const,
        relY: portY(i),
        color: portColor(name),
        system: name === HANDOFF || name === QUIESCENCE,
      }));
      const outPorts: PortSpec[] = outs.map((name, i) => ({
        name,
        side: "out" as const,
        relY: portY(ins.length + i),
        color: portColor(name),
        system: name === HANDOFF || name === QUIESCENCE,
      }));
      const node: FlowNode = {
        kind: e.kind,
        id: e.id,
        stages: e.stages,
        label: e.kind === "group" ? (topo.groups?.[e.id]?.label ?? e.id) : undefined,
        typeLabel:
          e.kind === "group" ? "team" : (e.stages[0].typeLabel ?? KIND_TYPE[e.stages[0].kind]),
        x,
        y,
        w: sized.w,
        h: sized.h,
        layer: l,
        row: r,
        inPorts,
        outPorts,
      };
      if (e.kind === "group") {
        node.members = e.stages.map((s, i) => ({
          stage: s,
          relY: sized.membersTop + i * MEMBER_H,
        }));
      }
      nodes.push(node);
      nodeById.set(e.id, node);
      y += sized.h + stackExtra(e) + V_GAP;
    });
  }

  // ── Observes / signal index / spawn rules ────────────────────────────────
  const emitSet = new Map<string, Set<string>>();
  stages.forEach((s) => emitSet.set(s.id, new Set(s.emits)));
  topo.edges.forEach((e) => {
    if (e.on && e.kind !== "seq") emitSet.get(e.from)?.add(e.on);
  });

  interface RawObserve {
    targetId: string;
    signal: string;
    condition?: string;
    bound?: string;
    judgeGated?: boolean;
  }
  const rawObserves: RawObserve[] = [];
  topo.edges.forEach((e) => {
    if (e.kind !== "seq" && e.on) {
      rawObserves.push({
        targetId: entityOf(e.to).id,
        signal: e.on,
        condition: e.condition,
        bound: e.bound,
        judgeGated: e.judgeGated,
      });
    } else if (e.kind === "seq" && e.condition?.includes("quiescence")) {
      rawObserves.push({ targetId: entityOf(e.to).id, signal: QUIESCENCE });
    }
  });
  const seenObs = new Set<string>();
  const observesList = rawObserves.filter((o) => {
    const key = `${o.targetId}:${o.signal}:${o.condition ?? ""}`;
    if (seenObs.has(key)) return false;
    seenObs.add(key);
    return true;
  });
  const observes: Record<string, ObserveSpec[]> = {};
  observesList.forEach((o) => {
    (observes[o.targetId] ??= []).push({
      signal: o.signal,
      condition: o.condition,
      bound: o.bound,
      judgeGated: o.judgeGated,
    });
  });

  const signals: SignalInfo[] = signalOrder.map((name) => ({
    name,
    color: signalColor[name],
    emitters: stages.filter((s) => emitSet.get(s.id)?.has(name)).map((s) => s.id),
    observers: [...new Set(observesList.filter((o) => o.signal === name).map((o) => o.targetId))],
  }));
  if (usesQuiescence) {
    signals.push({
      name: QUIESCENCE,
      color: signalColor[QUIESCENCE],
      emitters: [],
      observers: [
        ...new Set(observesList.filter((o) => o.signal === QUIESCENCE).map((o) => o.targetId)),
      ],
      system: true,
    });
  }

  const spawnRules: Record<string, string> = {};
  const groupRule = new Map<string, string>();
  entities
    .filter((e) => e.kind === "group")
    .forEach((g) => {
      const sigs = (observes[g.id] ?? [])
        .filter((o) => o.signal !== QUIESCENCE)
        .map((o) => o.signal);
      if (sigs.length > 0) groupRule.set(g.id, `spawns per ${[...new Set(sigs)].join(" ∨ ")}`);
    });

  stages.forEach((stage) => {
    if (stage.kind === "input") {
      spawnRules[stage.id] = "entry · once per run";
      return;
    }
    if (stage.group) {
      const def = topo.groups?.[stage.group];
      spawnRules[stage.id] = `sequential within ${def?.label ?? stage.group}`;
      return;
    }
    if (stage.perItem) {
      spawnRules[stage.id] = `× ${stage.perItem.replace(/^×\s*/, "")}`;
      return;
    }
    const obs = observes[stage.id] ?? [];
    const reactive = obs.filter((o) => o.signal !== QUIESCENCE);
    if (reactive.length > 0) {
      const loopBack = topo.edges.some(
        (e) => e.kind === "loop" && entityOf(e.to).id === stage.id && e.on,
      );
      const first = reactive[0];
      const cond = shortCond(first.condition, 26);
      spawnRules[stage.id] = loopBack
        ? `re-runs on ${first.signal}${cond ? ` · ${cond}` : ""}`
        : `spawned per ${first.signal}${cond ? ` · ${cond}` : ""}`;
      return;
    }
    if (obs.some((o) => o.signal === QUIESCENCE)) {
      spawnRules[stage.id] = "once · on quiescence";
      return;
    }
    const chained = topo.edges.find((e) => e.kind === "seq" && e.to === stage.id && e.on);
    spawnRules[stage.id] = chained ? `chained on ${chained.on}` : "once per run";
  });
  entities
    .filter((e) => e.kind === "group")
    .forEach((g) => {
      const node = nodeById.get(g.id);
      if (node) node.spawnRule = groupRule.get(g.id) ?? null;
    });

  // ── Extents ──────────────────────────────────────────────────────────────
  const colsInRow = (r: number) => Math.min(layerCount - r * layersPerRow, layersPerRow);
  const maxCols = Math.max(...Array.from({ length: rowCount }, (_, r) => colsInRow(r)));
  const gridRight = MARGIN_X + maxCols * (CARD_W + LAYER_GAP) - LAYER_GAP;
  const hasWrap = channelEdges.some((c) => c.edge.kind === "wrap");
  const wrapX = gridRight + WRAP_MARGIN;
  const wrapLeftX = MARGIN_X - 18;
  const bottomLanes = channelLaneCount[rowCount - 1] ?? 0;
  const gridBottom = rowY[rowCount - 1] + rowHeights[rowCount - 1];
  const height =
    gridBottom + (bottomLanes > 0 ? CHANNEL_BASE + bottomLanes * LANE_STEP : 0) + MARGIN_BOTTOM;
  const width = (hasWrap ? wrapX : gridRight) + MARGIN_X;

  // ── Edge geometry — anchored at ports ────────────────────────────────────
  const channelY = (ch: number, lane: number) =>
    ch === rowCount - 1
      ? gridBottom + CHANNEL_BASE + lane * LANE_STEP
      : rowY[ch] + rowHeights[ch] + CHANNEL_BASE + lane * LANE_STEP;
  const channelByEdge = new Map<RawEdge, ChannelEdge>();
  channelEdges.forEach((c) => channelByEdge.set(c.edge, c));

  const selfIdxCounter = new Map<string, number>();

  const edges: FlowEdge[] = rawEdges.map((e, i) => {
    const a = nodeById.get(e.from)!;
    const b = nodeById.get(e.to)!;
    const portName = portNameOf(e);
    const fromPort = a.outPorts.find((p) => p.name === portName);
    const toPort = b.inPorts.find((p) => p.name === portName);
    const fromRelY = fromPort?.relY ?? a.h / 2;
    const toRelY = toPort?.relY ?? b.h / 2;

    const routing: NonNullable<FlowEdge["_routing"]> = { fromRelY, toRelY };
    if (e.kind === "self") {
      const idx = selfIdxCounter.get(e.from) ?? 0;
      selfIdxCounter.set(e.from, idx + 1);
      routing.selfIdx = idx;
    } else if (e.kind === "loop") {
      const c = channelByEdge.get(e)!;
      routing.channelY = channelY(c.channel, c.lane);
    } else if (e.kind === "wrap") {
      const c = channelByEdge.get(e)!;
      routing.channelY = channelY(c.channel, c.lane);
      routing.wrapX = wrapX;
      routing.wrapLeftX = wrapLeftX;
    } else {
      routing.skip = Math.max(0, layerOf.get(e.to)! - layerOf.get(e.from)! - 1);
    }

    const edge: FlowEdge = {
      id: `e${i}`,
      from: e.from,
      to: e.to,
      kind: e.kind,
      signal: e.kind === "quiescence" ? QUIESCENCE : e.signal,
      color: e.kind === "quiescence" ? "var(--content-muted)" : colorOf(e.signal),
      condition: e.condition,
      bound: e.bound,
      judgeGated: e.judgeGated,
      path: "",
      chip: { x: 0, y: 0 },
      arrow: { x: 0, y: 0, dir: "right" },
      _routing: routing,
    };
    Object.assign(edge, routeEdge(edge, a, b));
    return edge;
  });

  return {
    nodes,
    edges,
    signals,
    width,
    height,
    spawnRules,
    observes,
    signalColor: { ...signalColor },
  };
}

// ─── Live rerouting — recompute edge paths after a node drag ─────────────────

/** Recompute path/chip/arrow for every edge from current node positions.
 *  Port offsets, channel Ys, and lane indices are frozen from layout; channel
 *  Ys clamp below dragged nodes so loops never cut through a card. */
export function rerouteEdges(
  edges: FlowEdge[],
  nodes: FlowNode[],
  overrides: Map<string, { x: number; y: number }>,
): FlowEdge[] {
  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  const pos = (id: string): NodePos => {
    const n = nodeById.get(id)!;
    const ov = overrides.get(id);
    return { x: ov?.x ?? n.x, y: ov?.y ?? n.y, w: n.w, h: n.h };
  };
  return edges.map((e) => ({ ...e, ...routeEdge(e, pos(e.from), pos(e.to)) }));
}
