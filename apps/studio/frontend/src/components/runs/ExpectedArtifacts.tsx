import Badge from "@/components/ui/Badge";
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

export interface ExpectedArtifactsProps {
  contract?: ArtifactContract | null;
  verification?: ArtifactVerification | null;
}

export default function ExpectedArtifacts({ contract, verification }: ExpectedArtifactsProps) {
  const expected = contract?.expected ?? [];
  if (!contract || expected.length === 0) return null;

  const producedById = new Map((verification?.produced ?? []).map((p) => [p.id, p]));
  const missingRequired = new Set((verification?.missing_required ?? []).map((p) => p.id));
  const missingOptional = new Set((verification?.missing_optional ?? []).map((p) => p.id));

  return (
    <div id="expected-artifacts" className="scroll-mt-24">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="text-label font-semibold text-content-primary">Expected artifacts</h2>
        <span className="rounded bg-surface-overlay px-1.5 py-0 font-mono text-[length:var(--t-xs)] text-content-muted">
          {expected.length}
        </span>
        {verification?.status && (
          <Badge tone={verificationTone(verification.status)}>
            Verified: {verification.status}
          </Badge>
        )}
      </div>
      <div className="rounded border border-edge bg-surface-raised px-3 py-2 shadow-card">
        <ul className="flex flex-col divide-y divide-edge-subtle">
          {expected.map((entry) => {
            const produced = producedById.get(entry.id);
            const missing = missingRequired.has(entry.id) || missingOptional.has(entry.id);
            const required = entry.required !== false;
            const statusTone = produced
              ? "ok"
              : missing && required
                ? "failed"
                : missing
                  ? "pending"
                  : "default";
            const statusLabel = produced
              ? `OK (${formatBytes(produced.size)})`
              : missing
                ? "MISSING"
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
                <div
                  className="min-w-0 truncate font-mono text-[length:var(--t-xs)] text-content-secondary"
                  title={entry.path}
                >
                  {entry.path}
                </div>
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
    </div>
  );
}
