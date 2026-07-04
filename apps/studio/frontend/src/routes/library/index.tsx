import { createFileRoute, Link } from "@tanstack/react-router";
import type { LinkProps } from "@tanstack/react-router";
import PageHeader from "@/components/PageHeader";

export const Route = createFileRoute("/library/")({
  component: LibraryPage,
});

interface KindEntry {
  label: string;
  description: string;
  href?: LinkProps["to"];
  note?: string;
}

const KINDS: KindEntry[] = [
  {
    label: "Script",
    description: "Multi-step playbooks that drive a run from start to finish.",
    href: "/playbooks",
  },
  {
    label: "Agent",
    description: "Profiles that pair a model with tools, a role, and defaults.",
    href: "/agents",
  },
  {
    label: "Schedule",
    description: "Cron, interval, and event triggers that fire runs on their own.",
    href: "/schedules",
  },
  {
    label: "Workflow",
    description: "Visual multi-step definitions authored on a canvas.",
    note: "Coming soon",
  },
  {
    label: "Skill",
    description: "Slash-command instructions an agent can pull in on demand.",
    href: "/skills",
  },
  {
    label: "Plugin",
    description: "Bundles of skills, agents, and hooks installed together.",
    href: "/plugins",
  },
  {
    label: "Engine",
    description: "Inference engine definitions and launch configuration.",
    href: "/engines",
  },
  {
    label: "Team",
    description: "Multi-agent coordination logs and rosters.",
    href: "/teams",
  },
];

function KindCard({ kind }: { kind: KindEntry }) {
  if (!kind.href) {
    return (
      <div className="flex flex-col gap-1.5 rounded-lg border border-edge border-dashed bg-surface-base p-4 text-content-muted">
        <div className="flex items-center justify-between gap-2">
          <span className="font-mono font-semibold text-[13px]">{kind.label}</span>
          <span className="inline-flex shrink-0 items-center rounded-full border border-edge bg-surface-overlay px-1.5 py-0.5 text-[10px] font-medium leading-none">
            {kind.note}
          </span>
        </div>
        <p className="text-meta">{kind.description}</p>
      </div>
    );
  }

  return (
    <Link
      to={kind.href}
      className="flex flex-col gap-1.5 rounded-lg border border-edge bg-surface-raised p-4 transition-all duration-150 hover:border-edge-strong hover:bg-surface-overlay"
    >
      <span className="font-mono font-semibold text-content-primary text-[13px]">{kind.label}</span>
      <p className="text-meta text-content-secondary">{kind.description}</p>
    </Link>
  );
}

function LibraryPage() {
  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader title="Library" subtitle="What can run, and how it's defined." density="tight" />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {KINDS.map((kind) => (
          <KindCard key={kind.label} kind={kind} />
        ))}
      </div>
    </main>
  );
}
