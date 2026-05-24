"use client";

import Link from "next/link";
import { Suspense, use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Button from "@/components/Button";
import PageHeader from "@/components/PageHeader";
import Timestamp from "@/components/Timestamp";
import { deleteProject, getProject, updateProject } from "@/lib/api";
import type { ProjectDetail } from "@/lib/types";
import { errors } from "@/lib/copy";

// Source badge colours mirror the list page
const SOURCE_CLASS: Record<string, string> = {
  config: "border-status-running/40 bg-status-running-bg text-status-running",
  override: "border-status-warning/40 bg-status-warning-bg text-status-warning",
  git: "border-status-selected/40 bg-status-selected-bg text-status-selected",
  studio: "border-status-success/40 bg-status-success-bg text-status-success",
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

function UsageTable({
  title,
  rows,
  nameLabel,
}: {
  title: string;
  rows: Array<{ name: string; count: number }>;
  nameLabel: string;
}) {
  if (rows.length === 0) {
    return (
      <section className="flex flex-col gap-2">
        <h2 className="font-mono text-[13px] font-semibold text-content-primary">{title}</h2>
        <p className="text-meta text-content-muted">None recorded.</p>
      </section>
    );
  }
  return (
    <section className="flex flex-col gap-2">
      <h2 className="font-mono text-[13px] font-semibold text-content-primary">{title}</h2>
      <div className="overflow-x-auto rounded border border-edge bg-surface-raised">
        <table className="w-full text-left text-body">
          <thead>
            <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
              <th className="px-3 py-2 font-medium">{nameLabel}</th>
              <th className="px-3 py-2 font-medium">Runs</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.name}
                className="border-b border-edge-subtle text-content-secondary hover:bg-surface-overlay transition-colors duration-100"
              >
                <td className="px-3 py-2 font-mono text-[12px] text-content-primary">{r.name}</td>
                <td className="px-3 py-2 tabular-nums">{r.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function EditForm({
  project,
  onSaved,
}: {
  project: ProjectDetail;
  onSaved: (updated: ProjectDetail) => void;
}) {
  const [github, setGithub] = useState(project.github ?? "");
  const [description, setDescription] = useState(project.description ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    setSaved(false);
    try {
      await updateProject(project.name, {
        github: github.trim() || undefined,
        description: description.trim() || undefined,
      });
      setSaved(true);
      onSaved({
        ...project,
        github: github.trim() || null,
        description: description.trim() || null,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="flex flex-col gap-3">
      <h2 className="font-mono text-[13px] font-semibold text-content-primary">Edit Project</h2>
      <form
        onSubmit={(e) => void handleSubmit(e)}
        className="flex flex-col gap-3 rounded border border-edge bg-surface-raised p-4"
      >
        <label className="flex flex-col gap-1">
          <span className="text-meta text-content-secondary font-medium">GitHub URL</span>
          <input
            type="url"
            value={github}
            onChange={(e) => setGithub(e.target.value)}
            placeholder="https://github.com/org/repo"
            className="h-8 rounded border border-edge bg-surface-base px-2.5 text-body text-content-primary placeholder:text-content-muted focus:border-interactive-primary focus:outline-none"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-meta text-content-secondary font-medium">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            placeholder="Short description..."
            className="rounded border border-edge bg-surface-base px-2.5 py-1.5 text-body text-content-primary placeholder:text-content-muted focus:border-interactive-primary focus:outline-none resize-none"
          />
        </label>
        {error && <p className="text-meta text-status-error">{error}</p>}
        {saved && <p className="text-meta text-status-success">Saved.</p>}
        <div className="flex justify-end">
          <Button variant="primary" type="submit" disabled={submitting}>
            {submitting ? "Saving..." : "Save"}
          </Button>
        </div>
      </form>
    </section>
  );
}

function DeleteButton({ name, onDeleted }: { name: string; onDeleted: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleDelete() {
    setDeleting(true);
    setError(null);
    try {
      await deleteProject(name);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete.");
      setDeleting(false);
      setConfirming(false);
    }
  }

  if (confirming) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-meta text-content-muted">Delete &ldquo;{name}&rdquo;?</span>
        <Button variant="danger" size="sm" disabled={deleting} onClick={() => void handleDelete()}>
          {deleting ? "Deleting..." : "Confirm"}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
          Cancel
        </Button>
        {error && <span className="text-meta text-status-error">{error}</span>}
      </div>
    );
  }

  return (
    <Button variant="danger" size="sm" onClick={() => setConfirming(true)}>
      Delete Project
    </Button>
  );
}

function ProjectDetailInner({ params }: { params: Promise<{ name: string }> }) {
  const { name } = use(params);
  const decodedName = decodeURIComponent(name);
  const router = useRouter();

  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const result = await getProject(decodedName);
        if (active) {
          setProject(result);
          setError(null);
        }
      } catch {
        if (active) setError(errors.loadProject);
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [decodedName]);

  if (loading) {
    return (
      <main className="mx-auto flex w-full max-w-5xl flex-col gap-5 px-4 py-6">
        <div className="flex flex-col gap-2">
          <div className="skeleton h-4 w-1/3 rounded" />
          <div className="skeleton h-6 w-2/3 rounded" />
        </div>
        <div className="skeleton h-32 w-full rounded" />
      </main>
    );
  }

  if (error || !project) {
    return (
      <main className="mx-auto flex w-full max-w-5xl flex-col gap-5 px-4 py-6">
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {error ?? "Project not found."}
        </div>
        <Link href="/projects" className="text-meta text-status-running hover:underline">
          Back to Projects
        </Link>
      </main>
    );
  }

  const agentRows = project.agents_used.map((a) => ({
    name: a.agent_name,
    count: a.run_count,
  }));
  const playbookRows = project.playbooks_used.map((p) => ({
    name: p.playbook_name,
    count: p.run_count,
  }));

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-6 px-4 py-6 animate-page-enter">
      <PageHeader
        breadcrumb={[
          <Link
            key="projects"
            href="/projects"
            className="hover:text-content-primary transition-colors"
          >
            Projects
          </Link>,
          <span key="name">{project.name}</span>,
        ]}
        title={project.name}
        badges={<SourceBadge source={project.source} />}
        density="tight"
      />

      {/* Metadata grid */}
      <div className="grid gap-4 sm:grid-cols-2">
        {/* Info card */}
        <div className="flex flex-col gap-3 rounded border border-edge bg-surface-raised p-4">
          {project.description && (
            <p className="text-body text-content-secondary">{project.description}</p>
          )}
          {project.github && (
            <div className="flex flex-col gap-0.5">
              <span className="text-meta text-content-muted font-medium">GitHub</span>
              <a
                href={project.github}
                target="_blank"
                rel="noopener noreferrer"
                className="text-body text-status-running hover:underline truncate"
              >
                {project.github}
              </a>
            </div>
          )}
          {project.path && (
            <div className="flex flex-col gap-0.5">
              <span className="text-meta text-content-muted font-medium">Path</span>
              <span
                className="font-mono text-[11px] text-content-secondary truncate"
                title={project.path}
              >
                {project.path}
              </span>
            </div>
          )}
          <div className="flex flex-col gap-0.5">
            <span className="text-meta text-content-muted font-medium">Last seen</span>
            <span className="text-meta text-content-secondary">
              <Timestamp value={project.last_seen_at ?? project.updated_at} />
            </span>
          </div>
        </div>

        {/* Stats card */}
        <div className="flex flex-col gap-3 rounded border border-edge bg-surface-raised p-4">
          <div className="flex flex-col gap-0.5">
            <span className="text-meta text-content-muted font-medium">Sessions</span>
            <Link
              href={`/runs?project=${encodeURIComponent(project.name)}`}
              className="text-xl font-semibold text-content-primary tabular-nums hover:text-status-running transition-colors"
            >
              {project.session_count}
            </Link>
          </div>
          {project.running_count > 0 && (
            <div className="flex flex-col gap-0.5">
              <span className="text-meta text-content-muted font-medium">Running now</span>
              <span className="text-xl font-semibold text-status-running tabular-nums">
                {project.running_count}
              </span>
            </div>
          )}
          <div className="flex flex-col gap-0.5">
            <span className="text-meta text-content-muted font-medium">Created</span>
            <span className="text-meta text-content-secondary">
              <Timestamp value={project.created_at} />
            </span>
          </div>
        </div>
      </div>

      {/* Usage tables */}
      <UsageTable title="Agents Used" rows={agentRows} nameLabel="Agent" />
      <UsageTable title="Playbooks Used" rows={playbookRows} nameLabel="Playbook" />

      {/* Editable controls */}
      {project.editable && (
        <EditForm project={project} onSaved={(updated) => setProject(updated)} />
      )}

      {/* Delete (studio-managed only — source === "studio") */}
      {project.source === "studio" && (
        <section className="flex flex-col gap-2">
          <h2 className="font-mono text-[13px] font-semibold text-content-primary">Danger Zone</h2>
          <div className="rounded border border-status-error/30 bg-status-error-bg p-4">
            <DeleteButton name={project.name} onDeleted={() => router.push("/projects")} />
          </div>
        </section>
      )}
    </main>
  );
}

export default function ProjectDetailPage({ params }: { params: Promise<{ name: string }> }) {
  return (
    <Suspense>
      <ProjectDetailInner params={params} />
    </Suspense>
  );
}
