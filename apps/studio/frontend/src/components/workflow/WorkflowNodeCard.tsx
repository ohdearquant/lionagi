import type { FlowNode } from "@/lib/designer/flow";
import type { WorkflowNodeKind } from "@/lib/api";

const KIND_GLYPH: Record<WorkflowNodeKind, string> = {
  input: "→",
  chat: "💬",
  parse: "⊞",
  fanout: "⋔",
  engine: "⊶",
};

const KIND_COLOR: Record<WorkflowNodeKind, string> = {
  input: "var(--accent)",
  chat: "var(--status-success)",
  parse: "var(--status-pending)",
  fanout: "var(--content-muted)",
  engine: "#a78bfa",
};

interface WorkflowNodeCardProps {
  node: FlowNode;
  /** Live canvas x (includes drag override). */
  x: number;
  /** Live canvas y (includes drag override). */
  y: number;
  selected: boolean;
  label: string;
  kind: WorkflowNodeKind;
}

export default function WorkflowNodeCard({
  node,
  x,
  y,
  selected,
  label,
  kind,
}: WorkflowNodeCardProps) {
  const glyph = KIND_GLYPH[kind] ?? "?";
  const color = KIND_COLOR[kind] ?? "var(--content-muted)";

  return (
    <div
      style={{
        position: "absolute",
        left: x,
        top: y,
        width: node.w,
        height: node.h,
        background: "var(--surface-raised)",
        border: `1.5px solid ${selected ? "var(--accent)" : "var(--edge)"}`,
        borderRadius: 8,
        boxShadow: selected ? "0 0 0 2px var(--accent)40" : "0 1px 3px rgba(0,0,0,0.3)",
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "0 14px",
        userSelect: "none",
        transition: "border-color 150ms, box-shadow 150ms",
      }}
    >
      <span
        style={{
          fontSize: 18,
          color,
          lineHeight: 1,
          minWidth: 22,
          textAlign: "center",
        }}
        aria-hidden="true"
      >
        {glyph}
      </span>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div
          style={{
            fontSize: "var(--t-xs)",
            fontFamily: "var(--font-ui)",
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: "0.09em",
            color,
            lineHeight: 1.2,
          }}
        >
          {kind}
        </div>
        <div
          style={{
            fontSize: "var(--t-sm)",
            color: "var(--content-primary)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            lineHeight: 1.4,
          }}
        >
          {label}
        </div>
      </div>
    </div>
  );
}
