import { useTranslations } from "use-intl";
import type { WorkflowNodeKind } from "@/lib/api";
import { useWorkflowDraft } from "./WorkflowDraftContext";

const PALETTE_ITEMS: Array<{ kind: WorkflowNodeKind; glyph: string }> = [
  { kind: "input", glyph: "→" },
  { kind: "chat", glyph: "💬" },
  { kind: "parse", glyph: "⊞" },
  { kind: "fanout", glyph: "⋔" },
  { kind: "engine", glyph: "⊶" },
  { kind: "gate", glyph: "◇" },
];

export default function WorkflowNodePalette() {
  const t = useTranslations("workflow");
  const { addNode } = useWorkflowDraft();

  return (
    <div
      style={{
        position: "absolute",
        left: 12,
        top: "50%",
        transform: "translateY(-50%)",
        zIndex: 10,
        display: "flex",
        flexDirection: "column",
        gap: 4,
        background: "var(--surface-raised)",
        border: "1px solid var(--edge)",
        borderRadius: 10,
        padding: "8px 6px",
        boxShadow: "0 2px 8px rgba(0,0,0,0.35)",
      }}
      aria-label={t("paletteLabel")}
    >
      {PALETTE_ITEMS.map(({ kind, glyph }) => (
        <button
          key={kind}
          type="button"
          title={t(`kinds.${kind}`)}
          aria-label={t(`kinds.${kind}`)}
          onClick={() => addNode(kind, 320, 160)}
          style={{
            width: 36,
            height: 36,
            borderRadius: 6,
            border: "1px solid var(--edge)",
            background: "var(--surface-overlay)",
            color: "var(--content-primary)",
            fontSize: 16,
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            transition: "background 100ms, border-color 100ms",
          }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLButtonElement).style.background =
              "var(--surface-hover, var(--surface-raised))";
            (e.currentTarget as HTMLButtonElement).style.borderColor = "var(--accent)";
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLButtonElement).style.background = "var(--surface-overlay)";
            (e.currentTarget as HTMLButtonElement).style.borderColor = "var(--edge)";
          }}
        >
          {glyph}
        </button>
      ))}
    </div>
  );
}
