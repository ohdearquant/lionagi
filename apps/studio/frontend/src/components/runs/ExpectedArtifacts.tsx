import { useState } from "react";
import Badge from "@/components/ui/Badge";
import { FileViewerModal } from "@/components/ui/Markdown";
import type { ArtifactContract, ArtifactVerification } from "@/lib/types";

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function verificationTone(status?: string | null): "ok" | "failed" | "pending" | "default" {
  if (status === "passed") return "ok";
  if (status === "failed") return "failed";
  if (status === "warning") return "pending";
  return "default";
}

function formatCheckedAt(checkedAt: number): string {
  return new Date(checkedAt * 1000).toLocaleString([], {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export interface ExpectedArtifactsProps {
  contract?: ArtifactContract | null;
  verification?: ArtifactVerification | null;
  /** When set, artifact paths open the run-file viewer in place. */
  runId?: string | null;
}

export default function ExpectedArtifacts({
  contract,
  verification,
  runId,
}: ExpectedArtifactsProps) {
  const [viewerPath, setViewerPath] = useState<string | null>(null);
  const expected = contract?.expected ?? [];
  if (!contract || expected.length === 0) return null;

  const notRecorded = verification?.status === "not_recorded";
  const result = verification && !notRecorded ? verification : null;
  const producedById = new Map((result?.produced ?? []).map((p) => [p.id, p]));
  const missingRequired = new Set((result?.missing_required ?? []).map((p) => p.id));
  const missingOptional = new Set((result?.missing_optional ?? []).map((p) => p.id));
  const changedSince = new Set(result?.changed_since_verification ?? []);
  const absentSince = new Set(result?.absent_since_verification ?? []);
  // A stored verdict without a "checked" mark never had its disk currency
  // read — a legacy payload, or a list row that skipped the filesystem
  // check — and must not be indistinguishable from a checked-clean result.
  const stalenessUnknown = !!result && !result.provisional && result.staleness_check !== "checked";

  return (
    <div id="expected-artifacts" className="scroll-mt-24">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-label font-semibold text-content-primary">Expected artifacts</h2>
        <span className="rounded bg-surface-overlay px-1.5 py-0 font-mono text-[length:var(--t-xs)] text-content-muted">
          {expected.length}
        </span>
        {notRecorded ? (
          <Badge tone="default">Verification not recorded</Badge>
        ) : result?.provisional ? (
          // Mid-run, a contract status is always "failed" until the last
          // artifact lands, which says nothing except that the run is not over.
          // Progress is the thing worth showing while it is still going.
          <Badge tone={producedById.size === expected.length ? "ok" : "pending"}>
            {producedById.size} of {expected.length} written
          </Badge>
        ) : (
          result?.status && (
            <Badge tone={verificationTone(result.status)}>Verified: {result.status}</Badge>
          )
        )}
        {result && !result.provisional && (
          // A recorded verdict is a snapshot taken at run completion — show
          // when it was taken rather than let the badge read as current state.
          <span className="text-[length:var(--t-xs)] text-content-muted">
            verified at completion, {formatCheckedAt(result.checked_at)}
          </span>
        )}
        {stalenessUnknown && <Badge tone="default">staleness unknown</Badge>}
        {absentSince.size > 0 && <Badge tone="pending">no longer present</Badge>}
        {changedSince.size > 0 && <Badge tone="pending">files changed since verification</Badge>}
      </div>
      <div className="rounded border border-edge bg-surface-raised px-3 py-2 shadow-card">
        <ul className="flex flex-col divide-y divide-edge-subtle">
          {expected.map((entry) => {
            const produced = producedById.get(entry.id);
            // A provisional reading is taken while the run is still going, so an
            // artifact that is not on disk yet has not been missed — it has not
            // been written yet. Only a recorded verdict can call one missing.
            const missing =
              !result?.provisional &&
              (missingRequired.has(entry.id) || missingOptional.has(entry.id));
            const required = entry.required !== false;
            const noLongerPresent = produced && absentSince.has(entry.id);
            const changedOnDisk = produced && !noLongerPresent && changedSince.has(entry.id);
            const statusTone = noLongerPresent
              ? "pending"
              : produced
                ? "ok"
                : missing && required
                  ? "failed"
                  : missing
                    ? "pending"
                    : "default";
            const statusLabel = noLongerPresent
              ? "NO LONGER PRESENT"
              : produced
                ? `OK (${formatBytes(produced.size)})${changedOnDisk ? " — changed since verification" : ""}`
                : missing
                  ? "MISSING"
                  : notRecorded
                    ? "NOT RECORDED"
                    : "PENDING";
            return (
              <li
                key={entry.id}
                className="grid gap-2 py-2 md:grid-cols-[88px_minmax(0,1fr)_minmax(0,1fr)_96px] md:items-start"
              >
                <Badge tone={required ? "failed" : "default"}>
                  {required ? "REQUIRED" : "OPTIONAL"}
                </Badge>
                <div className="min-w-0">
                  <div className="truncate font-mono text-[length:var(--t-xs)] font-semibold text-content-primary">
                    {entry.id}
                  </div>
                  {entry.description && (
                    <div className="mt-0.5 text-[length:var(--t-xs)] text-content-muted">
                      {entry.description}
                    </div>
                  )}
                </div>
                {runId ? (
                  <button
                    type="button"
                    onClick={() => setViewerPath(entry.path)}
                    title={entry.path}
                    className="min-w-0 truncate text-left font-mono text-[length:var(--t-xs)] text-accent underline decoration-dotted underline-offset-2 hover:text-accent/80"
                  >
                    {entry.path}
                  </button>
                ) : (
                  <div
                    className="min-w-0 truncate font-mono text-[length:var(--t-xs)] text-content-secondary"
                    title={entry.path}
                  >
                    {entry.path}
                  </div>
                )}
                <Badge tone={statusTone}>{statusLabel}</Badge>
                {entry.source && (
                  <div className="md:col-start-2 md:col-span-3 text-[length:var(--t-xs)] text-content-muted">
                    declared by:{" "}
                    <span className="font-mono text-content-secondary">{entry.source}</span>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </div>
      {runId && viewerPath && (
        <FileViewerModal runId={runId} path={viewerPath} onClose={() => setViewerPath(null)} />
      )}
    </div>
  );
}
