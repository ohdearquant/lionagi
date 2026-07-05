/**
 * ShowDetail — show detail pane for the history master-detail layout.
 *
 * Renders roll-up, plan summary, and plays table with an "Open full view →"
 * link to /shows/$topic/. Extracted from routes/shows/$topic/index.tsx.
 */

import { Link } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import StatusPill from "@/components/ui/StatusPill";
import Timestamp from "@/components/ui/Timestamp";
import Duration from "@/components/ui/Duration";
import SectionLabel from "@/components/ui/SectionLabel";
import { getShow } from "@/lib/api";
import { IconChevronDown, IconChevronUp } from "@/components/ui/icons";
import type { ShowDetail as ShowDetailData, PlayMeta } from "@/lib/types";
import type { ShowSummary } from "@/lib/types";

interface HistoryEntry {
  key: string;
  kind: string;
  name: string;
  status: string;
  startedAt: number;
  endedAt?: number | null;
  raw: unknown;
}

interface Props {
  entry: HistoryEntry;
}

type Play = ShowDetailData["plays"][number];

function toSeconds(value: string | undefined | null): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms / 1000;
}

function playDurationSec(meta: PlayMeta): number | null {
  const start = toSeconds(meta.started_at);
  const end = toSeconds(meta.ended_at) ?? toSeconds(meta.merged_at);
  if (start == null) return null;
  if (end == null) return -1;
  const d = end - start;
  return d < 0 ? -1 : d;
}

function extractSummary(showMd: string | null): Record<string, string> {
  if (!showMd) return {};
  const out: Record<string, string> = {};
  const lines = showMd.split("\n").slice(0, 60);
  const labelRe =
    /^\s*(?:[-*]\s*)?\**\s*(Goal|Status|Blockers?|Next action|Next steps?|Owner|Started|Updated|Progress)\**\s*:\s*(.+?)\s*$/i;
  for (const line of lines) {
    const m = line.match(labelRe);
    if (m) {
      const key = m[1].toLowerCase();
      if (!out[key]) out[key] = m[2].trim();
    }
  }
  return out;
}

export default function ShowDetail({ entry }: Props) {
  const show_raw = entry.raw as ShowSummary;
  const topic = show_raw.topic;
  const [show, setShow] = useState<ShowDetailData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await getShow(topic);
      setShow(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load show");
    } finally {
      setLoading(false);
    }
  }, [topic]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset stale state before async fetch; setState fires synchronously in effect body only, not in the async load callback
    setShow(null);
    setError(null);
    setLoading(true);
    setExpanded(null);
    void load();
  }, [load]);

  const rollup = useMemo(() => {
    if (!show) return null;
    const failed = show.plays.filter(
      (p) =>
        p.verdict?.gate_passed === false ||
        (typeof p.meta.exit_code === "number" && p.meta.exit_code !== 0),
    );
    const running = show.plays.filter(
      (p) => p.meta.status === "running" || p.meta.status === "pending",
    );
    const merged = show.plays.filter((p) => Boolean(p.meta.merged_at));
    return { total: show.plays.length, failed, running, merged };
  }, [show]);

  const summary = useMemo(() => extractSummary(show?.show_md ?? null), [show]);

  if (loading) {
    return (
      <div className="flex flex-1 items-center justify-center text-[length:var(--t-sm)] text-content-muted">
        Loading…
      </div>
    );
  }

  if (error || !show) {
    return (
      <div className="p-4 text-[length:var(--t-sm)] text-status-failure">
        {error ?? "Not found"}
      </div>
    );
  }

  const latestPlay = show.plays.at(-1);
  const latestStatus = latestPlay?.meta.status ?? "—";
  const nextValue = summary["next action"] ?? summary["next step"] ?? summary["next steps"];

  return (
    <div className="flex h-full min-h-0 flex-col overflow-y-auto bg-surface-base">
      {/* Header */}
      <div className="flex shrink-0 items-center gap-3 border-b border-edge px-3 py-2.5">
        <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-base)] font-medium text-content-primary">
          {topic}
        </span>
        <StatusPill value={latestStatus} kind="lifecycle" />
      </div>

      <div className="flex flex-col gap-4 p-3">
        {/* Roll-up grid */}
        {rollup && (
          <div className="grid grid-cols-2 gap-2">
            {[
              { label: "Plays", value: String(rollup.total) },
              {
                label: "Running",
                value: String(rollup.running.length),
                tone: rollup.running.length > 0 ? "running" : undefined,
              },
              {
                label: "Failed",
                value: String(rollup.failed.length),
                tone: rollup.failed.length > 0 ? "failed" : undefined,
              },
              { label: "Merged", value: String(rollup.merged.length) },
            ].map(({ label, value, tone }) => (
              <div key={label} className="rounded border border-edge bg-surface-raised px-2.5 py-2">
                <div className="text-[length:var(--t-xs)] uppercase tracking-[0.06em] text-content-muted">
                  {label}
                </div>
                <div
                  className={`mt-0.5 font-data tabular-nums text-[length:var(--t-base)] ${
                    tone === "running"
                      ? "text-status-running"
                      : tone === "failed"
                        ? "text-status-failure"
                        : "text-content-primary"
                  }`}
                >
                  {value}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Plan summary — goal / status / next */}
        {(summary.goal || summary.status || nextValue) && (
          <section>
            <div className="mb-1.5">
              <SectionLabel>Plan</SectionLabel>
            </div>
            <div className="flex flex-col gap-1.5 rounded border border-edge bg-surface-raised px-2.5 py-2 text-[length:var(--t-sm)]">
              {summary.goal && (
                <div>
                  <span className="text-content-muted">Goal </span>
                  <span className="text-content-primary">{summary.goal}</span>
                </div>
              )}
              {summary.status && (
                <div>
                  <span className="text-content-muted">Status </span>
                  <span className="text-content-primary">{summary.status}</span>
                </div>
              )}
              {nextValue && (
                <div>
                  <span className="text-content-muted">Next </span>
                  <span className="text-content-primary">{nextValue}</span>
                </div>
              )}
            </div>
          </section>
        )}

        {/* Plays table */}
        {show.plays.length > 0 && (
          <section>
            <div className="mb-1.5">
              <SectionLabel count={show.plays.length}>Plays</SectionLabel>
            </div>
            <div className="flex flex-col rounded border border-edge">
              {show.plays.map((play: Play, i: number) => {
                const dur = playDurationSec(play.meta);
                const isExpanded = expanded === play.name;
                return (
                  <div key={play.name} className={i > 0 ? "border-t border-edge" : undefined}>
                    <button
                      type="button"
                      onClick={() => setExpanded(isExpanded ? null : play.name)}
                      className={`flex w-full items-center gap-2 px-2.5 py-2 text-left text-[length:var(--t-sm)] hover:bg-surface-overlay ${isExpanded ? "bg-surface-overlay" : ""}`}
                    >
                      <StatusPill value={play.meta.status} kind="lifecycle" />
                      <span className="min-w-0 flex-1 truncate font-data text-content-primary">
                        {play.name}
                      </span>
                      <span className="shrink-0 font-data tabular-nums text-content-muted">
                        <Duration value={dur} />
                      </span>
                      <span className="flex items-center text-content-muted">
                        {isExpanded ? (
                          <IconChevronUp size={9} strokeWidth={2.25} />
                        ) : (
                          <IconChevronDown size={9} strokeWidth={2.25} />
                        )}
                      </span>
                    </button>
                    {isExpanded && (
                      <div className="border-t border-edge px-2.5 py-2 text-[length:var(--t-xs)] text-content-secondary">
                        <div className="flex flex-wrap gap-x-4 gap-y-1">
                          {play.verdict && (
                            <span>
                              Gate:{" "}
                              <StatusPill
                                kind="verdict"
                                value={play.verdict.gate_passed ? "passed" : "rejected"}
                              />
                            </span>
                          )}
                          <span>
                            Exit: <span className="font-data">{play.meta.exit_code ?? "—"}</span>
                          </span>
                          <span>
                            Updated:{" "}
                            <Timestamp
                              value={
                                play.updated_at ??
                                play.meta.ended_at ??
                                play.meta.started_at ??
                                null
                              }
                            />
                          </span>
                          {play.session_id && (
                            <Link
                              to="/fleet"
                              search={{ s: play.session_id }}
                              className="text-accent underline"
                            >
                              session →
                            </Link>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {/* Open full view */}
        <Link
          to="/shows/$topic"
          params={{ topic }}
          className="inline-flex items-center gap-1 text-[length:var(--t-sm)] text-accent underline"
        >
          {topic} — open full view →
        </Link>
      </div>
    </div>
  );
}
