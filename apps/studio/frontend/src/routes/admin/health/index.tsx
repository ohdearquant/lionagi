import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import PageHeader from "@/components/PageHeader";
import Timestamp from "@/components/Timestamp";
import { getAdminDoctor } from "@/lib/api";
import type { AdminDoctorResponse, PhantomReason } from "@/lib/api";

export const Route = createFileRoute("/admin/health/")({
  component: AdminHealthPage,
});

function formatBytes(value: number): string {
  if (value === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(value) / Math.log(1024));
  return `${(value / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

function reasonLabel(reason: PhantomReason): string {
  switch (reason) {
    case "process_dead":
      return "Process dead";
    case "missing_artifacts":
      return "Missing artifacts";
    case "stale_lock":
      return "Stale lock";
  }
}

function DbHealthStrip({ doctor }: { doctor: AdminDoctorResponse }) {
  const h = doctor.db_health;
  return (
    <div className="flex flex-wrap gap-x-5 gap-y-1 rounded border border-edge bg-surface-overlay px-4 py-2.5 text-meta text-content-muted">
      <span className="uppercase tracking-[0.08em] text-content-muted">DB Health</span>
      <span>
        <span className="tabular-nums text-content-secondary">{formatBytes(h.size_bytes)}</span>{" "}
        state DB
      </span>
      <span>
        <span className="tabular-nums text-content-secondary">{formatBytes(h.wal_bytes)}</span> WAL
      </span>
      <span>
        <span className="tabular-nums text-content-secondary">{formatBytes(h.wal_pending)}</span>{" "}
        WAL pending
      </span>
      <span>
        Checked <Timestamp value={doctor.diagnostic_run_at} exact />
      </span>
    </div>
  );
}

function AdminHealthPage() {
  const [doctor, setDoctor] = useState<AdminDoctorResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const d = await getAdminDoctor();
      setDoctor(d);
      setError(null);
    } catch {
      setError("Failed to load diagnostics");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- refresh() calls setState inside async callbacks, not synchronously in the effect body
    void refresh();
  }, []);

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader title="Admin Health" subtitle="Read-only system diagnostics" density="tight" />

      {error && (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-content-primary">
          {error}
        </div>
      )}

      {loading ? (
        <div className="py-8 text-center text-meta text-content-muted">Loading...</div>
      ) : (
        doctor && <DbHealthStrip doctor={doctor} />
      )}

      {doctor && (
        <section>
          <div className="mb-2.5 flex items-center justify-between">
            <h2 className="text-label font-semibold text-content-primary">
              Phantom sessions
              <span className="ml-2 rounded bg-surface-overlay px-1.5 py-0.5 font-mono text-meta tabular-nums text-content-muted">
                {doctor.phantom_sessions.length}
              </span>
            </h2>
            <Link
              to="/admin/maintenance"
              className="text-meta text-content-muted underline-offset-2 transition-colors duration-150 hover:text-content-primary hover:underline"
            >
              Manage in Maintenance →
            </Link>
          </div>
          {doctor.phantom_sessions.length === 0 ? (
            <div className="rounded border border-status-success/25 bg-status-success-bg px-4 py-4 text-body text-content-primary shadow-card">
              No phantom sessions detected.
            </div>
          ) : (
            <div className="overflow-x-auto rounded border border-edge bg-surface-raised shadow-card">
              {/* Read-only mirror of the phantom table on /admin/maintenance.
                  Health shows what's happening; Maintenance is where the
                  mutating prune actions live (ADR-0032 §6). No checkboxes,
                  no action buttons here. */}
              <table className="w-full text-left text-body">
                <thead>
                  <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
                    <th className="px-3 py-2.5 font-medium">Session</th>
                    <th className="px-3 py-2.5 font-medium">Reason</th>
                    <th className="px-3 py-2.5 font-medium">Started</th>
                  </tr>
                </thead>
                <tbody>
                  {doctor.phantom_sessions.map((p) => (
                    <tr
                      key={p.session_id}
                      className="border-b border-edge-subtle text-content-secondary"
                    >
                      <td className="px-3 py-2">
                        <div className="font-medium text-content-primary">{p.playbook ?? "—"}</div>
                        <div className="font-mono text-meta text-content-muted">
                          {p.session_id.slice(-8)}
                        </div>
                      </td>
                      <td className="px-3 py-2">
                        <span className="rounded border border-status-error/40 bg-status-error-bg px-1.5 py-0.5 text-meta text-content-primary">
                          {reasonLabel(p.reason)}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-meta text-content-muted">
                        <Timestamp value={p.started_at ?? null} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </main>
  );
}
