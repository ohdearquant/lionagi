import { useMemo, useState } from "react";
import { useTranslations } from "use-intl";
import { FileViewerModal } from "@/components/ui/Markdown";
import type { RunFileAccess, RunFileSummary } from "@/lib/types";

const FILES_PER_REVEAL = 20;
type Filter = "all" | RunFileAccess;

export default function RunFilesSection({
  runId,
  summary,
}: {
  runId: string;
  summary?: RunFileSummary | null;
}) {
  const t = useTranslations("history.detail");
  const [filter, setFilter] = useState<Filter>("all");
  const [visibleCount, setVisibleCount] = useState(FILES_PER_REVEAL);
  const [openPath, setOpenPath] = useState<string | null>(null);
  const items = useMemo(() => summary?.items ?? [], [summary]);
  const filtered = useMemo(
    () => (filter === "all" ? items : items.filter((item) => item.access.includes(filter))),
    [filter, items],
  );
  const visible = filtered.slice(0, visibleCount);
  const safeTotal = Math.max(summary?.total ?? items.length, items.length);

  const selectFilter = (next: Filter) => {
    setFilter(next);
    setVisibleCount(FILES_PER_REVEAL);
  };

  return (
    <section id="run-files" aria-labelledby="run-files-heading" className="scroll-mt-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h3
          id="run-files-heading"
          className="text-[length:var(--t-xs)] font-semibold uppercase tracking-wider text-content-muted"
        >
          {t("sectionFiles")}
          <span className="ml-1.5 font-mono font-normal text-content-secondary">{safeTotal}</span>
        </h3>
        {items.length > 0 && (
          <div className="ml-auto flex items-center gap-1" aria-label={t("fileFiltersLabel")}>
            {(
              [
                ["all", t("fileFilterAll")],
                ["write", t("fileFilterWritten")],
                ["read", t("fileFilterRead")],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                aria-pressed={filter === value}
                onClick={() => selectFilter(value)}
                className={`rounded-full border px-2 py-0.5 font-mono text-[length:var(--t-xs)] transition-colors ${
                  filter === value
                    ? "border-accent/60 bg-accent/10 text-accent"
                    : "border-edge text-content-muted hover:border-edge-strong hover:text-content-secondary"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        )}
      </div>

      {items.length === 0 ? (
        <div className="rounded border border-edge bg-surface-raised px-4 py-3 text-sm text-content-muted">
          {t("noFiles")}
        </div>
      ) : (
        <div className="overflow-hidden rounded border border-edge bg-surface-raised shadow-card">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-edge-subtle px-3 py-2 text-[length:var(--t-xs)] text-content-muted">
            <span aria-live="polite">
              {t("fileRecentWindow", { visible: visible.length, total: safeTotal })}
            </span>
            {(summary?.redacted_count ?? 0) > 0 && (
              <span className="text-status-warning">
                {t("fileRedacted", { count: summary?.redacted_count ?? 0 })}
              </span>
            )}
          </div>
          <ul className="divide-y divide-edge-subtle">
            {visible.map((item) => (
              <li
                key={item.path}
                data-testid="run-file-item"
                className="flex min-w-0 items-center gap-2 px-3 py-2"
              >
                {item.openable ? (
                  <button
                    type="button"
                    data-path={item.path}
                    title={item.path}
                    aria-label={t("fileOpen", { path: item.path })}
                    onClick={() => setOpenPath(item.path)}
                    className="min-w-0 flex-1 truncate text-left font-mono text-[length:var(--t-xs)] text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    {item.path}
                  </button>
                ) : (
                  <span
                    data-path={item.path}
                    title={t("filePreviewUnavailable")}
                    className="min-w-0 flex-1 truncate font-mono text-[length:var(--t-xs)] text-content-secondary"
                  >
                    {item.path}
                  </span>
                )}
                <span className="flex shrink-0 items-center gap-1">
                  {item.access.includes("write") && (
                    <span
                      data-access="write"
                      className="rounded bg-status-success-bg px-1.5 py-0.5 text-[length:var(--t-xs)] text-status-success"
                    >
                      {t("fileFilterWritten")}
                    </span>
                  )}
                  {item.access.includes("read") && (
                    <span
                      data-access="read"
                      className="rounded bg-status-running-bg px-1.5 py-0.5 text-[length:var(--t-xs)] text-status-running"
                    >
                      {t("fileFilterRead")}
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
          {visible.length < filtered.length && (
            <div className="border-t border-edge-subtle px-3 py-2">
              <button
                type="button"
                onClick={() => setVisibleCount((count) => count + FILES_PER_REVEAL)}
                className="rounded border border-edge px-2.5 py-1 font-mono text-[length:var(--t-xs)] text-content-secondary transition-colors hover:border-accent/50 hover:text-content-primary"
              >
                {t("fileShowMore")}
              </button>
            </div>
          )}
        </div>
      )}

      {openPath && (
        <FileViewerModal runId={runId} path={openPath} onClose={() => setOpenPath(null)} />
      )}
    </section>
  );
}
