/**
 * KnobChip — an inline-editable config chip rendered directly on the canvas
 * (stage nodes, edge labels, header strip). Click to edit in place; Enter or
 * blur commits, Escape cancels. The `nodrag nopan` classes keep ReactFlow
 * from hijacking pointer events while editing.
 */
import { useEffect, useRef, useState } from "react";

export interface KnobChipProps {
  label: string;
  value: string;
  placeholder: string;
  onCommit: (value: string) => void;
  numeric?: boolean;
  mono?: boolean;
  /** Render emphasized (accent border) — used for required-but-empty knobs. */
  attention?: boolean;
  width?: number;
}

export default function KnobChip({
  label,
  value,
  placeholder,
  onCommit,
  numeric,
  mono = true,
  attention,
  width = 120,
}: KnobChipProps) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(value);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  const commit = () => {
    setEditing(false);
    if (text !== value) onCommit(text);
  };

  if (editing) {
    return (
      <span className="nodrag nopan inline-flex items-center gap-1">
        <span className="font-data text-[length:var(--t-xs)] text-content-muted">{label}</span>
        <input
          ref={inputRef}
          type={numeric ? "number" : "text"}
          value={text}
          min={numeric ? 1 : undefined}
          max={numeric ? 100 : undefined}
          onChange={(e) => setText(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") {
              setText(value);
              setEditing(false);
            }
            e.stopPropagation();
          }}
          placeholder={placeholder}
          className={`rounded-[3px] border border-accent bg-surface-overlay px-[5px] py-px text-[length:var(--t-xs)] text-content-primary outline-none ${mono ? "font-data" : "font-ui"}`}
          style={{ width: numeric ? 52 : width }}
        />
      </span>
    );
  }

  return (
    <button
      type="button"
      className={`nodrag nopan inline-flex cursor-pointer items-center gap-1 whitespace-nowrap rounded-[3px] border bg-surface-overlay px-1.5 py-px font-data text-[length:var(--t-xs)] transition-[border-color] duration-100 ${
        attention && !value ? "border-accent" : "border-edge"
      } ${value ? "text-content-secondary" : "text-content-muted"}`}
      onClick={(e) => {
        e.stopPropagation();
        setText(value);
        setEditing(true);
      }}
      title={`${label} — click to edit`}
    >
      <span className="text-content-muted">{label}</span>
      <span style={{ fontWeight: value ? 600 : 400 }}>{value || placeholder}</span>
      <svg
        width="8"
        height="8"
        viewBox="0 0 12 12"
        fill="none"
        aria-hidden="true"
        className="shrink-0 opacity-50"
      >
        <path
          d="M8.5 1.5l2 2L4 10H2V8l6.5-6.5z"
          stroke="currentColor"
          strokeWidth="1.2"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  );
}
