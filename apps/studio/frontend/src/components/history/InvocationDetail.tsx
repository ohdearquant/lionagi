/**
 * InvocationSection — embeddable invocation block rendered inside RunDetail.
 * Fetches its own data; renders nothing on load/error (auxiliary, non-blocking).
 */

import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { useTranslations } from "use-intl";
import StatusPill from "@/components/ui/StatusPill";
import Duration from "@/components/ui/Duration";
import SectionLabel from "@/components/ui/SectionLabel";
import OutcomeRenderer from "@/components/outcomes/OutcomeRenderer";
import { getInvocation } from "@/lib/api";
import type { InvocationDetail as InvocationDetailData } from "@/lib/api";

interface Props {
  invocationId: string;
  currentSessionId?: string;
}

export default function InvocationSection({ invocationId, currentSessionId }: Props) {
  const t = useTranslations("history.detail");
  const [data, setData] = useState<InvocationDetailData | null>(null);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));

  useEffect(() => {
    let active = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset stale state before async fetch
    setData(null);
    async function load() {
      try {
        const d = await getInvocation(invocationId);
        if (active) setData(d);
      } catch {
        // silent — auxiliary section
      }
    }
    void load();
    const tick = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 30000);
    return () => {
      active = false;
      clearInterval(tick);
    };
  }, [invocationId]);

  if (!data) return null;

  const dur = (data.ended_at ?? now) - data.started_at;
  const siblings = data.sessions.filter((s) => s.id !== currentSessionId);

  return (
    <div id="run-invocation" className="scroll-mt-4">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="text-label font-semibold text-content-primary">{t("sectionInvocation")}</h2>
      </div>
      <div className="rounded border border-edge bg-surface-raised px-3 py-2.5">
        {/* Skill + status + meta row */}
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-data font-medium text-content-primary">/{data.skill}</span>
          <StatusPill value={data.status} />
          {data.plugin && (
            <span className="font-data text-[length:var(--t-xs)] text-content-muted">
              {data.plugin}
            </span>
          )}
          <span className="font-data tabular-nums text-[length:var(--t-xs)] text-content-muted">
            <Duration value={dur} />
          </span>
        </div>

        {/* Sibling runs */}
        {siblings.length > 0 && (
          <div className="mt-2.5 border-t border-edge pt-2">
            <div className="mb-1">
              <SectionLabel>{t("invocationSiblingRuns")}</SectionLabel>
            </div>
            <div className="flex flex-col divide-y divide-edge-subtle rounded border border-edge">
              {siblings.map((s) => (
                <Link
                  key={s.id}
                  to="/fleet"
                  search={{ s: s.id }}
                  className="flex items-center gap-2 px-2.5 py-1.5 hover:bg-surface-overlay"
                >
                  <StatusPill value={s.status ?? "pending"} />
                  <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-sm)] text-content-primary hover:underline">
                    {s.name || s.agent_name || s.playbook_name || s.id.slice(0, 8)}
                  </span>
                  {s.model && (
                    <span className="shrink-0 font-data text-[length:var(--t-xs)] text-content-muted">
                      {s.model}
                    </span>
                  )}
                </Link>
              ))}
            </div>
          </div>
        )}

        {/* Artifacts */}
        {data.artifacts.length > 0 && (
          <div className="mt-2.5 border-t border-edge pt-2">
            <div className="mb-1.5">
              <SectionLabel count={data.artifacts.length}>{t("invocationArtifacts")}</SectionLabel>
            </div>
            <div className="flex flex-col gap-2">
              {data.artifacts.map((a) => (
                <OutcomeRenderer key={a.id} artifact={a} />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
