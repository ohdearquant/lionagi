"use client";

import { useEffect, useState } from "react";
import PageHeader from "@/components/PageHeader";
import Timestamp from "@/components/Timestamp";
import { getAdminDoctor } from "@/lib/api";
import type { AdminDoctorResponse } from "@/lib/api";

function formatBytes(value: number): string {
  if (value === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(value) / Math.log(1024));
  return `${(value / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
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

export default function AdminHealthPage() {
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
        <div className="rounded border border-edge bg-surface-overlay px-4 py-3 text-body text-content-secondary">
          <span className="font-medium text-content-primary">Phantom sessions:</span>{" "}
          <span className="tabular-nums">{doctor.phantom_sessions.length}</span> detected
        </div>
      )}
    </main>
  );
}
