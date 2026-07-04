import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import Button from "@/components/Button";
import PageHeader from "@/components/PageHeader";
import Timestamp from "@/components/Timestamp";
import { getAdminDoctor, pruneAdmin, runMaintenance } from "@/lib/api";
import type {
  AdminDoctorResponse,
  MaintenanceAction,
  MaintenanceResult,
  PhantomReason,
} from "@/lib/api";
import { confirmPhantomPrune, empty, errors } from "@/lib/copy";

export const Route = createFileRoute("/admin/maintenance/")({
  component: AdminMaintenancePage,
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

function formatMaintenanceResult(result: MaintenanceResult): string {
  if (result.action === "vacuum") {
    if (result.status === "skipped") return "Vacuum skipped: no state database exists yet.";
    return `Vacuum complete (status: ${result.status ?? "ok"}).`;
  }
  if (result.action === "checkpoint") {
    if (result.busy == null && result.log_pages == null && result.checkpointed == null) {
      return "Checkpoint skipped: no state database exists yet.";
    }
    return `Checkpoint complete (busy: ${result.busy}, log_pages: ${result.log_pages}, checkpointed: ${result.checkpointed}).`;
  }
  if (result.action === "prune") {
    return `Prune complete: ${result.sessions_pruned ?? 0} session(s), ${result.runs_pruned ?? 0} run(s) removed.`;
  }
  return "Done.";
}

function AdminMaintenancePage() {
  const [doctor, setDoctor] = useState<AdminDoctorResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pruning, setPruning] = useState(false);
  const [maintenanceRunning, setMaintenanceRunning] = useState<MaintenanceAction | null>(null);
  const [maintenanceResult, setMaintenanceResult] = useState<string | null>(null);
  const [maintenanceError, setMaintenanceError] = useState<string | null>(null);

  async function refresh() {
    try {
      const d = await getAdminDoctor();
      setDoctor(d);
      setError(null);
    } catch {
      setError(errors.loadDiagnostics);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- refresh() calls setState inside async callbacks, not synchronously in the effect body
    void refresh();
  }, []);

  function toggleSelect(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function handlePruneSelected() {
    if (selected.size === 0) return;
    const confirmed = window.confirm(confirmPhantomPrune(selected.size, false));
    if (!confirmed) return;
    setPruning(true);
    try {
      await pruneAdmin({ session_ids: Array.from(selected) });
      setSelected(new Set());
      await refresh();
    } catch {
      setError(errors.prune);
    } finally {
      setPruning(false);
    }
  }

  async function handlePruneAll() {
    const count = (doctor?.phantom_sessions ?? []).length;
    const confirmed = window.confirm(confirmPhantomPrune(count, true));
    if (!confirmed) return;
    setPruning(true);
    try {
      await pruneAdmin({ all_phantom: true });
      setSelected(new Set());
      await refresh();
    } catch {
      setError(errors.pruneAll);
    } finally {
      setPruning(false);
    }
  }

  async function handleMaintenance(action: MaintenanceAction) {
    setMaintenanceRunning(action);
    setMaintenanceResult(null);
    setMaintenanceError(null);
    try {
      const result = await runMaintenance(action);
      setMaintenanceResult(formatMaintenanceResult(result));
    } catch (err) {
      // fetchJson always throws an Error (backend detail when present, else
      // "Request failed: <status>"), so the copy-string fallback only fires
      // for non-Error throws (network-layer failures).
      const backendMsg = err instanceof Error ? err.message : undefined;
      const fallback =
        action === "vacuum"
          ? errors.maintenanceVacuum
          : action === "checkpoint"
            ? errors.maintenanceCheckpoint
            : errors.maintenancePrune;
      setMaintenanceError(backendMsg ?? fallback);
    } finally {
      setMaintenanceRunning(null);
    }
  }

  const phantoms = doctor?.phantom_sessions ?? [];

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6 animate-page-enter">
      <PageHeader
        title="Admin Maintenance"
        subtitle="Prune phantom sessions and system maintenance"
        density="tight"
      />

      {error && (
        <div className="rounded border border-status-failure/30 bg-status-failure/10 px-3 py-2 text-body text-content-primary">
          {error}
        </div>
      )}

      {doctor && <DbHealthStrip doctor={doctor} />}

      <section>
        <div className="mb-2.5 flex items-center justify-between">
          <h2 className="text-label font-semibold text-content-primary">
            Phantom sessions
            <span className="ml-2 rounded bg-surface-overlay px-1.5 py-0.5 font-mono text-meta tabular-nums text-content-muted">
              {phantoms.length}
            </span>
          </h2>
          <div className="flex gap-2">
            <Button
              variant="danger"
              size="sm"
              disabled={selected.size === 0 || pruning}
              onClick={() => void handlePruneSelected()}
            >
              Prune selected
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={phantoms.length === 0 || pruning}
              onClick={() => void handlePruneAll()}
            >
              Prune all phantom
            </Button>
          </div>
        </div>

        {loading ? (
          <div className="py-8 text-center text-meta text-content-muted">Loading...</div>
        ) : phantoms.length === 0 ? (
          <div className="rounded border border-status-success/25 bg-status-success/10 px-4 py-4 text-body text-content-primary">
            {empty.phantomSessions}
          </div>
        ) : (
          <div className="overflow-x-auto rounded border border-edge bg-surface-raised">
            <table className="w-full text-left text-body">
              <thead>
                <tr className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
                  <th className="px-3 py-2.5 w-8" />
                  <th className="px-3 py-2.5 font-medium">Session</th>
                  <th className="px-3 py-2.5 font-medium">Reason</th>
                  <th className="px-3 py-2.5 font-medium">Started</th>
                </tr>
              </thead>
              <tbody>
                {phantoms.map((p) => (
                  <tr
                    key={p.session_id}
                    className="border-b border-edge-hairline text-content-secondary transition-colors duration-100 hover:bg-surface-overlay"
                  >
                    <td className="px-3 py-2">
                      <input
                        type="checkbox"
                        checked={selected.has(p.session_id)}
                        onChange={() => toggleSelect(p.session_id)}
                        className="rounded border-edge"
                      />
                    </td>
                    <td className="px-3 py-2">
                      <div className="font-medium text-content-primary">{p.playbook ?? "—"}</div>
                      <div className="font-mono text-meta text-content-muted">
                        {p.session_id.slice(-8)}
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      <span className="rounded border border-status-failure/40 bg-status-failure/10 px-1.5 py-0.5 text-meta text-content-primary">
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

      <section>
        <div className="mb-2.5 flex items-center justify-between">
          <h2 className="text-label font-semibold text-content-primary">DB maintenance</h2>
        </div>

        {maintenanceResult && (
          <div className="mb-3 rounded border border-status-success/30 bg-status-success/10 px-3 py-2 text-body text-content-primary">
            {maintenanceResult}
          </div>
        )}
        {maintenanceError && (
          <div className="mb-3 rounded border border-status-failure/30 bg-status-failure/10 px-3 py-2 text-body text-content-primary">
            {maintenanceError}
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          <Button
            variant="secondary"
            size="sm"
            disabled={maintenanceRunning !== null}
            onClick={() => void handleMaintenance("checkpoint")}
          >
            {maintenanceRunning === "checkpoint" ? "Running..." : "Checkpoint WAL"}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={maintenanceRunning !== null}
            onClick={() => void handleMaintenance("prune")}
          >
            {maintenanceRunning === "prune" ? "Running..." : "Prune old data"}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={maintenanceRunning !== null}
            onClick={() => void handleMaintenance("vacuum")}
          >
            {maintenanceRunning === "vacuum" ? "Running..." : "Vacuum DB"}
          </Button>
        </div>
        <p className="mt-2 text-meta text-content-muted">
          Checkpoint flushes the WAL file. Prune removes terminal sessions older than the configured
          keep_days. Vacuum reclaims freed pages (run after prune).
        </p>
      </section>
    </main>
  );
}
