/**
 * Detail pane for a built-in playbook template or a user's own installed
 * playbook (the two "workflow" sub-kinds that back the Studio Workflows page
 * per DESIGN-BRIEF §3). Both read from the same declarative shape via
 * rawToDeclarative(); the only difference is which endpoint the raw YAML
 * comes from and whether "Clone to customize" is offered.
 *
 * These playbooks are agent+prompt templates, not node/edge graphs — there is
 * no DAG to draw. Rather than fabricate a fake diagram, this renders the
 * structured metadata (agent, effort, inputs, prompt) that actually exists.
 */

import { useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import {
  getBuiltinPlaybookRaw,
  getWorkerRaw,
  installBuiltinPlaybook,
  launchPlaybook,
  listRuns,
  rawToDeclarative,
} from "@/lib/api";
import type { DeclarativePlaybookData } from "@/lib/types";
import type { RunSummary } from "@/lib/types";
import DrawerBackButton from "@/components/ui/DrawerBackButton";
import DrawerHeader from "@/components/ui/DrawerHeader";
import SectionLabel from "@/components/ui/SectionLabel";
import Button from "@/components/ui/Button";
import StatusPill from "@/components/ui/StatusPill";
import Duration from "@/components/ui/Duration";
import { useToast } from "@/components/ui/Toast";

interface PlaybookTemplateDetailProps {
  name: string;
  isBuiltin: boolean;
  onBack?: () => void;
  /** Fired after a successful "Clone to customize" — parent reloads + reselects. */
  onCloned?: (name: string) => void;
}

const KNOWN_STATUSES = new Set([
  "running",
  "completed",
  "failed",
  "cancelled",
  "pending",
  "queued",
  "timed_out",
  "aborted",
  "skipped",
]);

export function PlaybookTemplateDetail({
  name,
  isBuiltin,
  onBack,
  onCloned,
}: PlaybookTemplateDetailProps) {
  const t = useTranslations("library.template");
  const tDrawer = useTranslations("library.drawer");
  const tStatus = useTranslations("history.status");
  const tMission = useTranslations("mission");
  const { toast } = useToast();

  const [data, setData] = useState<DeclarativePlaybookData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [cloning, setCloning] = useState(false);

  useEffect(() => {
    let alive = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset stale state before fetching a new selection
    setLoading(true);
    setError(null);
    setData(null);

    const fetchRaw = isBuiltin ? getBuiltinPlaybookRaw(name) : getWorkerRaw(name);
    fetchRaw
      .then((raw) => {
        if (!alive) return;
        setData(rawToDeclarative(name, raw.data ?? {}));
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : tDrawer("notFound"));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });

    return () => {
      alive = false;
    };
  }, [name, isBuiltin, tDrawer]);

  const loadRuns = () => {
    let alive = true;
    setRunsLoading(true);
    listRuns({ playbook: name, per_page: 10 })
      .then((res) => {
        if (alive) setRuns(res.runs);
      })
      .catch(() => {
        if (alive) setRuns([]);
      })
      .finally(() => {
        if (alive) setRunsLoading(false);
      });
    return () => {
      alive = false;
    };
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps, react-hooks/set-state-in-effect -- loadRuns is re-created each render intentionally; its synchronous setRunsLoading(true) resets stale state before the new fetch resolves
  useEffect(() => loadRuns(), [name]);

  const statusLabel = (status: string): string | undefined => {
    const s = status.toLowerCase();
    return KNOWN_STATUSES.has(s) ? tStatus(s as Parameters<typeof tStatus>[0]) : undefined;
  };

  async function handleRun() {
    if (running) return;
    setRunning(true);
    try {
      if (isBuiltin) await installBuiltinPlaybook(name);
      await launchPlaybook(name);
      toast(t("launched", { name }), "success");
      // Best-effort refresh — the new run may not be visible immediately.
      setTimeout(loadRuns, 1500);
    } catch {
      toast(t("launchFailed"), "error");
    } finally {
      setRunning(false);
    }
  }

  async function handleClone() {
    if (cloning) return;
    setCloning(true);
    try {
      await installBuiltinPlaybook(name);
      toast(t("cloned"), "success");
      onCloned?.(name);
    } catch {
      toast(t("cloneFailed"), "error");
    } finally {
      setCloning(false);
    }
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-[length:var(--t-sm)] text-content-muted">
        {tDrawer("loading")}
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="p-4 text-[length:var(--t-sm)] text-status-failure">
        {error ?? tDrawer("notFound")}
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {onBack && <DrawerBackButton onClick={onBack}>{tDrawer("back")}</DrawerBackButton>}

      <DrawerHeader
        name={name}
        badge={isBuiltin ? t("badge") : undefined}
        trailing={
          <>
            {isBuiltin && (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => void handleClone()}
                disabled={cloning}
              >
                {cloning ? t("cloning") : t("clone")}
              </Button>
            )}
            <Button size="sm" variant="primary" onClick={() => void handleRun()} disabled={running}>
              {running ? t("running") : t("run")}
            </Button>
          </>
        }
      />

      <div className="flex-1 overflow-auto p-4">
        <div className="flex flex-col gap-4">
          {data.description && (
            <p className="text-[length:var(--t-sm)] text-content-secondary">{data.description}</p>
          )}

          {/* Metadata chips — the structured facts we actually have, in lieu
              of a step/DAG diagram these prompt-driven playbooks don't have. */}
          <div className="flex flex-wrap gap-x-5 gap-y-2 text-[length:var(--t-xs)]">
            {data.agent && (
              <div className="flex items-center gap-1.5">
                <span className="text-content-muted">{t("agent")}</span>
                <span className="font-data text-content-primary">{data.agent}</span>
              </div>
            )}
            {data.effort && (
              <div className="flex items-center gap-1.5">
                <span className="text-content-muted">{t("effort")}</span>
                <span className="font-data text-content-primary">{data.effort}</span>
              </div>
            )}
            {data.maxOps != null && (
              <div className="flex items-center gap-1.5">
                <span className="text-content-muted">{t("maxOps")}</span>
                <span className="font-data text-content-primary">{data.maxOps}</span>
              </div>
            )}
          </div>

          {/* Inputs */}
          <div>
            <SectionLabel className="mb-1.5">{t("inputsTitle")}</SectionLabel>
            {data.args.length > 0 ? (
              <div className="overflow-hidden rounded border border-edge">
                <table
                  className="w-full text-left text-[length:var(--t-xs)]"
                  style={{ borderCollapse: "collapse" }}
                >
                  <thead>
                    <tr className="border-b border-edge bg-surface-raised text-content-muted">
                      <th className="px-2 py-1.5 font-medium">{t("fieldName")}</th>
                      <th className="px-2 py-1.5 font-medium">{t("fieldType")}</th>
                      <th className="px-2 py-1.5 font-medium">{t("fieldDefault")}</th>
                      <th className="px-2 py-1.5 font-medium">{t("fieldHelp")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.args.map((a, i) => (
                      <tr
                        key={a.name}
                        className="bg-surface-raised"
                        style={{
                          borderTop: i === 0 ? undefined : "1px solid var(--edge-hairline)",
                        }}
                      >
                        <td className="px-2 py-1.5 font-data text-content-primary">{a.name}</td>
                        <td className="px-2 py-1.5 font-data text-content-secondary">{a.type}</td>
                        <td className="px-2 py-1.5 font-data text-content-secondary">
                          {a.default || "—"}
                        </td>
                        <td className="px-2 py-1.5 text-content-secondary">{a.help || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : data.argumentHint ? (
              <p className="text-[length:var(--t-sm)] text-content-muted">
                {t("argumentHint", { field: data.argumentHint })}
              </p>
            ) : (
              <p className="text-[length:var(--t-sm)] text-content-muted">{t("noInputs")}</p>
            )}
          </div>

          {/* Prompt */}
          {data.prompt && (
            <div>
              <SectionLabel className="mb-1.5">{t("promptTitle")}</SectionLabel>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border border-edge bg-surface-raised p-3 font-data text-[length:var(--t-xs)] text-content-secondary">
                {data.prompt}
              </pre>
            </div>
          )}

          {/* Recent runs — status-only (no verdict data source exists yet). */}
          <div>
            <SectionLabel className="mb-1.5">{t("recentRunsTitle")}</SectionLabel>
            {runsLoading ? (
              <p className="text-[length:var(--t-sm)] text-content-muted">{tDrawer("loading")}</p>
            ) : runs.length === 0 ? (
              <p className="text-[length:var(--t-sm)] text-content-muted">
                {tMission("recent.empty")}
              </p>
            ) : (
              <div className="overflow-hidden rounded border border-edge">
                {runs.map((run, i) => (
                  <div
                    key={run.run_id}
                    className="flex items-center gap-3 bg-surface-raised px-3 py-1.5"
                    style={{ borderTop: i === 0 ? undefined : "1px solid var(--edge-hairline)" }}
                  >
                    <StatusPill
                      value={run.status}
                      kind="lifecycle"
                      label={statusLabel(run.status)}
                    />
                    <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] text-content-secondary">
                      {run.run_id.slice(-12)}
                    </span>
                    <span className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted">
                      {/* Only terminal runs have a stable duration — a still-running
                          row would need a live-ticking clock to render honestly,
                          which this static list doesn't have; it shows "—" instead. */}
                      <Duration
                        value={
                          run.started_at != null && run.ended_at != null
                            ? run.ended_at - run.started_at
                            : null
                        }
                      />
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
