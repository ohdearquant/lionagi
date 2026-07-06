import type React from "react";
import {
  IconAgent,
  IconFanout,
  IconPlaybook,
  IconSkill,
  IconPlugin,
  IconEngine,
} from "@/components/ui/icons";

export type LibraryKind = "agent" | "workflow" | "playbook" | "skill" | "plugin" | "engine";

const KIND_COLORS: Record<LibraryKind, string> = {
  agent: "var(--status-running)",
  workflow: "#4DBFB4",
  playbook: "var(--accent)",
  skill: "var(--status-success)",
  plugin: "#9B8AF5",
  engine: "var(--content-muted)",
};

const KIND_ICONS: Record<LibraryKind, React.ComponentType<{ className?: string }>> = {
  agent: IconAgent,
  workflow: IconFanout,
  playbook: IconPlaybook,
  skill: IconSkill,
  plugin: IconPlugin,
  engine: IconEngine,
};

export function KindBadge({ kind }: { kind: LibraryKind }) {
  const Icon = KIND_ICONS[kind];
  const color = KIND_COLORS[kind];
  return (
    <span
      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[length:var(--t-xs)] uppercase tracking-[0.08em] font-medium"
      style={{
        color,
        background: `color-mix(in srgb, ${color} 9%, transparent)`,
        border: `1px solid color-mix(in srgb, ${color} 19%, transparent)`,
      }}
    >
      <Icon className="h-2.5 w-2.5" />
      {kind}
    </span>
  );
}
