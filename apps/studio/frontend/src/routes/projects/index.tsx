import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { Suspense, useEffect, useState } from "react";
import Button from "@/components/Button";
import PageHeader from "@/components/PageHeader";
import Timestamp from "@/components/Timestamp";
import { createProject, listProjects, type ProjectListResponse } from "@/lib/api";
import type { ProjectSummary } from "@/lib/types";
import { errors } from "@/lib/copy";

export const Route = createFileRoute("/projects/")({
  component: ProjectsPage,
});

// Source badge colours follow the same design vocabulary as StatusPill
// but are simpler (no icon, static colours per source type).
const SOURCE_CLASS: Record<string, string> = {
  config: "border-status-running/40 bg-status-running/10 text-status-running",
  override: "border-status-pending/40 bg-status-pending/10 text-status-pending",
  git: "border-edge-strong/40 bg-surface-overlay text-edge-strong",
  studio: "border-status-success/40 bg-status-success/10 text-status-success",
};

function SourceBadge({ source }: { source: string }) {
  const cls =
    SOURCE_CLASS[source.toLowerCase()] ?? "border-edge bg-surface-overlay text-content-secondary";
  return (
    <span
      className={[
        "inline-flex items-center rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none tracking-wide",
        cls,
      ].join(" ")}
    >
      {source}
    </span>
  );
}

function CreateProjectModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [github, setGithub] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) {
      setError(errors.nameRequired);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await createProject({
        name: name.trim(),
        github: github.trim() || undefined,
        description: description.trim() || undefined,
      });
      onCreated();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create project.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events -- TODO(#1020 follow-up): modal backdrop dismiss; keyboard Escape handled by inner dialog
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-md rounded-lg border border-edge bg-surface-raised p-5">
        <h2 className="mb-4 font-mono text-base font-semibold text-content-primary">New Project</h2>
        <form onSubmit={(e) => void handleSubmit(e)} className="flex flex-col gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-meta text-content-secondary font-medium">
              Name <span className="text-status-failure">*</span>
            </span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-project"
              className="h-8 rounded border border-edge bg-surface-base px-2.5 text-body text-content-primary placeholder:text-content-muted focus:border-accent focus:outline-none"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-meta text-content-secondary font-medium">GitHub URL</span>
            <input
              type="url"
              value={github}
              onChange={(e) => setGithub(e.target.value)}
              placeholder="https://github.com/org/repo"
              className="h-8 rounded border border-edge bg-surface-base px-2.5 text-body text-content-primary placeholder:text-content-muted focus:border-accent focus:outline-none"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-meta text-content-secondary font-medium">Description</span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              placeholder="Short description..."
              className="rounded border border-edge bg-surface-base px-2.5 py-1.5 text-body text-content-primary placeholder:text-content-muted focus:border-accent focus:outline-none resize-none"
            />
          </label>
          {error && <p className="text-meta text-status-failure">{error}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={onClose} type="button">
              Cancel
            </Button>
            <Button variant="primary" type="submit" disabled={submitting}>
              {submitting ? "Creating..." : "Create"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

function ProjectCard({ project }: { project: ProjectSummary }) {
  const navigate = useNavigate();
  return (
    <div
      className="flex flex-col gap-2.5 rounded-lg border border-edge bg-surface-raised p-4 transition-all duration-150 hover:border-edge-strong hover:bg-surface-overlay cursor-pointer"
      onClick={() => void navigate({ to: "/projects/$name", params: { name: project.name } })}
      role="link"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          void navigate({
            to: "/projects/$name",
            params: { name: project.name },
          });
        }
      }}
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-2">
        <span className="font-mono font-semibold text-content-primary truncate text-[13px]">
          {project.name}
        </span>
        <SourceBadge source={project.source} />
      </div>

      {/* Description */}
      {project.description && (
        <p className="text-meta text-content-secondary truncate">{project.description}</p>
      )}

      {/* GitHub link */}
      {project.github && (
        <a
          href={project.github}
          target="_blank"
          rel="noopener noreferrer"
          className="text-meta text-status-running hover:underline truncate"
          onClick={(e) => e.stopPropagation()}
        >
          {project.github.replace(/^https?:\/\//, "")}
        </a>
      )}

      {/* Path */}
      {project.path && (
        <p className="font-mono text-[10px] text-content-muted truncate" title={project.path}>
          {project.path}
        </p>
      )}

      {/* Stats row */}
      <div className="flex items-center gap-3 text-meta text-content-muted">
        <span>
          <span className="font-medium text-content-secondary">{project.session_count}</span>{" "}
          session{project.session_count !== 1 ? "s" : ""}
        </span>
        {project.running_count > 0 && (
          <span className="text-status-running font-medium">{project.running_count} running</span>
        )}
        <span className="ml-auto">
          <Timestamp value={project.last_seen_at ?? project.updated_at} />
        </span>
      </div>
    </div>
  );
}

function UnassignedCard({ count }: { count: number }) {
  return (
    <Link
      to="/runs"
      search={{ project: "" }}
      className="flex flex-col gap-2 rounded-lg border border-edge border-dashed bg-surface-base p-4 text-content-muted transition-all duration-150 hover:border-edge-strong hover:bg-surface-overlay"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono font-semibold text-[13px]">Unassigned</span>
        <span className="inline-flex items-center rounded-full border border-edge bg-surface-overlay px-1.5 py-0.5 text-[10px] font-medium leading-none">
          {count}
        </span>
      </div>
      <p className="text-meta">Sessions not associated with any project.</p>
    </Link>
  );
}

function SkeletonCard() {
  return (
    <div className="flex flex-col gap-2.5 rounded-lg border border-edge bg-surface-raised p-4">
      <div className="skeleton h-4 w-2/3 rounded" />
      <div className="skeleton h-3 w-full rounded" />
      <div className="skeleton h-3 w-1/2 rounded" />
    </div>
  );
}

function ProjectsPageInner() {
  const [data, setData] = useState<ProjectListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);

  async function load() {
    try {
      const result = await listProjects();
      setData(result);
      setError(null);
    } catch {
      setError(errors.loadProjects);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async data fetch sets state in callback, not synchronously
    void load();
  }, []);

  const projects = data?.projects ?? [];
  const unassignedCount = data?.unassigned_count ?? 0;

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader
        title="Projects"
        subtitle="Workspace contexts grouping sessions and runs"
        density="tight"
        badges={
          !loading && data ? (
            <span className="text-meta text-content-muted tabular-nums">
              {projects.length} project{projects.length !== 1 ? "s" : ""}
            </span>
          ) : null
        }
        actions={
          <Button variant="primary" size="sm" onClick={() => setShowModal(true)}>
            + New Project
          </Button>
        }
      />

      {error && (
        <div className="rounded border border-status-failure/30 bg-status-failure/10 px-3 py-2 text-body text-status-failure">
          {error}
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {loading ? (
          <>
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
          </>
        ) : projects.length === 0 && unassignedCount === 0 ? (
          <div className="col-span-full py-14 text-center text-body text-content-muted">
            <span className="block mb-1">No projects found.</span>
            <span className="text-meta">
              Create one or run <code className="font-mono">li agent</code> inside a project
              directory.
            </span>
          </div>
        ) : (
          <>
            {projects.map((p) => (
              <ProjectCard key={p.name} project={p} />
            ))}
            <UnassignedCard count={unassignedCount} />
          </>
        )}
      </div>

      {showModal && (
        <CreateProjectModal onClose={() => setShowModal(false)} onCreated={() => void load()} />
      )}
    </main>
  );
}

function ProjectsPage() {
  return (
    <Suspense>
      <ProjectsPageInner />
    </Suspense>
  );
}
