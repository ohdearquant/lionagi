import { createFileRoute, Link } from "@tanstack/react-router";
import type { LinkProps } from "@tanstack/react-router";
import PageHeader from "@/components/PageHeader";

export const Route = createFileRoute("/system/")({
  component: SystemPage,
});

interface SectionEntry {
  label: string;
  description: string;
  href: LinkProps["to"];
}

const SECTIONS: SectionEntry[] = [
  {
    label: "Health",
    description: "Database size, WAL state, connections, and staleness sweeps.",
    href: "/admin/health",
  },
  {
    label: "Maintenance",
    description: "Checkpoint, prune, and vacuum actions, each with a confirmation.",
    href: "/admin/maintenance",
  },
  {
    label: "Projects",
    description: "The inventory of workspace contexts known to the daemon.",
    href: "/projects",
  },
];

function SectionCard({ section }: { section: SectionEntry }) {
  return (
    <Link
      to={section.href}
      className="flex flex-col gap-1.5 rounded-lg border border-edge bg-surface-raised p-4 transition-all duration-150 hover:border-edge-strong hover:bg-surface-overlay"
    >
      <span className="font-mono font-semibold text-content-primary text-[13px]">
        {section.label}
      </span>
      <p className="text-meta text-content-secondary">{section.description}</p>
    </Link>
  );
}

function SystemPage() {
  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader title="System" subtitle="Is the machine healthy?" density="tight" />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {SECTIONS.map((section) => (
          <SectionCard key={section.label} section={section} />
        ))}
      </div>
    </main>
  );
}
