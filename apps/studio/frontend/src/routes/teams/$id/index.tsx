import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import PageHeader from "@/components/PageHeader";
import Timestamp from "@/components/Timestamp";
import { getTeam } from "@/lib/api";
import type { TeamDetail } from "@/lib/api";
import { errors } from "@/lib/copy";

export const Route = createFileRoute("/teams/$id/")({
  component: TeamDetailPage,
});

function JsonTree({ value }: { value: unknown }) {
  return (
    <pre className="overflow-auto rounded border border-edge bg-surface-overlay p-3 font-mono text-meta text-content-secondary max-h-96">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function TeamMessages({ messages }: { messages: unknown[] }) {
  return (
    <div className="flex flex-col gap-2">
      {messages.map((msg, i) => {
        const m = msg as Record<string, unknown>;
        return (
          <div key={i} className="rounded border border-edge bg-surface-overlay p-3 text-body">
            <div className="mb-1 flex items-center gap-3 text-meta text-content-muted">
              <span className="font-mono">{String(m.from ?? "?")}</span>
              <span>→</span>
              <span className="font-mono">{String(m.to ?? "?")}</span>
              {m.timestamp != null && <Timestamp value={m.timestamp as string | number | null} />}
            </div>
            {m.content != null && <div className="text-content-secondary">{String(m.content)}</div>}
          </div>
        );
      })}
    </div>
  );
}

function TeamDetailPage() {
  const { id } = Route.useParams();
  const teamId = id;

  const [team, setTeam] = useState<TeamDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getTeam(teamId)
      .then((d) => {
        if (active) {
          setTeam(d);
          setError(null);
        }
      })
      .catch(() => {
        if (active) setError(errors.teamNotFound);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [teamId]);

  if (loading) {
    return (
      <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
        <div className="text-meta text-content-muted">Loading...</div>
      </main>
    );
  }

  if (error || !team) {
    return (
      <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {error ?? "Team not found"}
        </div>
        <Link to="/teams" className="text-meta text-status-running hover:underline">
          Back to teams
        </Link>
      </main>
    );
  }

  const messages = Array.isArray(team.messages) ? team.messages : [];
  const members = Array.isArray(team.members) ? team.members : [];

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader title={String(team.name ?? teamId)} subtitle="Teams" density="tight" />

      <div className="flex flex-wrap gap-x-5 gap-y-1 rounded border border-edge bg-surface-overlay px-4 py-2.5 text-meta text-content-muted">
        <span>
          <span className="font-mono text-content-secondary">{String(team.id ?? teamId)}</span>
        </span>
        <span>
          <span className="tabular-nums text-content-secondary">{members.length}</span> members
        </span>
        {messages.length > 0 && (
          <span>
            <span className="tabular-nums text-content-secondary">{messages.length}</span> messages
          </span>
        )}
        {team.created_at != null && (
          <span>
            Created <Timestamp value={team.created_at as string | number} />
          </span>
        )}
      </div>

      {messages.length > 0 && (
        <section>
          <h2 className="mb-2 text-label font-semibold text-content-primary">Messages</h2>
          <TeamMessages messages={messages} />
        </section>
      )}

      <section>
        <h2 className="mb-2 text-label font-semibold text-content-primary">Full JSON</h2>
        <JsonTree value={team} />
      </section>

      <Link to="/teams" className="text-meta text-status-running hover:underline">
        Back to teams
      </Link>
    </main>
  );
}
