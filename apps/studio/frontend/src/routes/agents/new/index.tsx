import { createFileRoute, Link } from "@tanstack/react-router";
import { notImplemented } from "@/lib/copy";

export const Route = createFileRoute("/agents/new/")({
  component: NewAgentPage,
});

// POST /api/agents/{name} returns 501. This page previously rendered an
// AgentProfileForm whose Create Agent button called that route. The form is
// replaced with a hold-message until the backend implements agent creation.

function NewAgentPage() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-col gap-6 px-4 py-12">
      <header className="flex flex-col gap-2 border-b border-edge pb-4">
        <Link to="/agents" className="text-meta text-content-muted hover:text-content-primary">
          &larr; agents
        </Link>
        <h1 className="text-xl font-semibold text-content-primary">New Agent</h1>
      </header>

      <div className="rounded-lg border border-edge bg-surface-raised p-6 text-center">
        <p className="text-body text-content-secondary">{notImplemented.newAgent}</p>
        <p className="mt-3 font-mono text-meta text-content-muted">li agent --help</p>
      </div>
    </main>
  );
}
