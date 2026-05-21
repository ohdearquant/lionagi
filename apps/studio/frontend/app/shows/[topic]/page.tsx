"use client";

import Link from "next/link";
import dynamic from "next/dynamic";
import React, { use, useCallback, useEffect, useMemo, useState } from "react";
import Button from "@/components/Button";
import Duration from "@/components/Duration";
import PageHeader from "@/components/PageHeader";
import StatusPill from "@/components/StatusPill";
import Timestamp from "@/components/Timestamp";
import { getShow, streamShow } from "@/lib/api";
import type { PlayMeta, ShowDetail, ShowEvent } from "@/lib/types";

const Markdown = dynamic(() => import("@/components/Markdown"), { ssr: false });
const PlayDag = dynamic(() => import("./components/PlayDag"), { ssr: false });

type Play = ShowDetail["plays"][number];

type ReconcileFlag = "exit_mismatch" | "missing_exit" | null;

function reconcileFlag(meta: PlayMeta): ReconcileFlag {
  if (typeof meta.exit_code === "number" && meta.exit_code !== 0) return "exit_mismatch";
  if (meta.exit_code === undefined && Boolean(meta.merged_at)) return "missing_exit";
  return null;
}

function toSeconds(value: string | undefined | null): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms / 1000;
}

function playDurationSec(meta: PlayMeta): number | null {
  const start = toSeconds(meta.started_at);
  const end = toSeconds(meta.ended_at) ?? toSeconds(meta.merged_at);
  if (start == null) return null;
  if (end == null) return -1; // still running
  const d = end - start;
  return d < 0 ? -1 : d;
}

// Pull a structured summary from the leading lines of _show.md (heuristic).
// We look for "Goal: ...", "Status: ...", "Blockers: ..." patterns near the top.
function extractSummary(showMd: string | null): Record<string, string> {
  if (!showMd) return {};
  const out: Record<string, string> = {};
  const lines = showMd.split("\n").slice(0, 60);
  const labelRe =
    /^\s*(?:[-*]\s*)?\**\s*(Goal|Status|Blockers?|Next action|Next steps?|Owner|Started|Updated|Progress)\**\s*:\s*(.+?)\s*$/i;
  for (const line of lines) {
    const m = line.match(labelRe);
    if (m) {
      const key = m[1].toLowerCase().replace(/s$/, "");
      if (!out[key]) out[key] = m[2].trim();
    }
  }
  return out;
}

export default function ShowDetailPage({ params }: { params: Promise<{ topic: string }> }) {
  const { topic: rawTopic } = use(params);
  const topic = decodeURIComponent(rawTopic);
  const [show, setShow] = useState<ShowDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showPlan, setShowPlan] = useState(false);
  const [lastRefreshed, setLastRefreshed] = useState<number | null>(null);
  // per-play raw-data section toggle (keyed by play name)
  const [rawExpanded, setRawExpanded] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    try {
      const data = await getShow(topic);
      setShow(data);
      setError(null);
      setLastRefreshed(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load show");
    }
  }, [topic]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load is async; setState only fires after await, never synchronously
    void load();
  }, [load]);

  // open plan panel by default for active/running shows
  useEffect(() => {
    if (!show) return;
    const s = show.status ?? show.plays.at(-1)?.meta.status ?? "";
    if (s === "active" || s === "running") {
      const id = setTimeout(() => setShowPlan(true), 0);
      return () => clearTimeout(id);
    }
  }, [show]);

  useEffect(() => {
    if (!live) return;
    return streamShow(topic, (event: ShowEvent) => {
      // H-FE-5: "done" is terminal — stop live mode; streamShow already closes
      // the EventSource, but we also flip the toggle so the UI reflects it.
      if (event.type === "done") {
        setLive(false);
        return;
      }
      void load();
    });
  }, [live, load, topic]);

  const latestPlay = show?.plays.at(-1);
  const latestStatus = latestPlay?.meta.status ?? "—";

  // Health roll-up across plays
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

  return (
    <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-4 px-4 py-6 text-content-primary animate-page-enter">
      <PageHeader
        density="tight"
        breadcrumb={[
          <Link key="shows" href="/shows" className="hover:text-content-primary">
            shows
          </Link>,
          <span key="topic" className="text-content-secondary truncate">
            {topic}
          </span>,
        ]}
        title={topic}
        badges={<StatusPill value={latestStatus} kind="lifecycle" />}
        actions={
          <div className="flex items-center gap-2">
            <span className="text-meta text-content-muted tabular-nums">
              {lastRefreshed ? (
                <>
                  refreshed <Timestamp value={lastRefreshed} />
                </>
              ) : null}
            </span>
            <Button
              variant="toggle"
              size="sm"
              active={live}
              leading={live ? "●" : "○"}
              onClick={() => setLive((v) => !v)}
            >
              {live ? "Live on" : "Live off"}
            </Button>
          </div>
        }
      />

      {error && (
        <div className="rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-body text-status-error">
          {error}
        </div>
      )}

      {!show && !error && (
        <div className="py-10 text-center text-body text-content-muted">Loading...</div>
      )}

      {show && (
        <>
          {/* Structured operational summary — first thing engineers see */}
          <ShowSummaryPanel summary={summary} rollup={rollup} />

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)] lg:items-start">
            {/* Left: Plan & decisions (collapsed by default, open for active/running) */}
            <section className="flex min-w-0 flex-col gap-2">
              <button
                type="button"
                onClick={() => setShowPlan((v) => !v)}
                className="flex items-center justify-between text-left text-label font-semibold text-content-primary hover:text-content-secondary transition-colors"
              >
                <span>Plan &amp; decisions</span>
                <span className="text-content-muted text-body ml-2">{showPlan ? "▴" : "▾"}</span>
              </button>
              {showPlan ? (
                <div className="overflow-auto rounded border border-edge bg-surface-raised p-4 max-h-[calc(100vh-18rem)]">
                  {show.show_md ? (
                    <Markdown>{show.show_md}</Markdown>
                  ) : (
                    <p className="text-body text-content-muted">No _show.md found.</p>
                  )}
                </div>
              ) : (
                <div className="rounded border border-edge bg-surface-overlay px-3 py-3 text-body text-content-secondary">
                  <p>
                    Operational summary above. Expand <em>Plan &amp; decisions</em> for the full
                    markdown plan.
                  </p>
                </div>
              )}
            </section>

            {/* Right: plays table — sticky on large screens */}
            <section className="flex min-w-0 flex-col gap-2 lg:sticky lg:top-16 lg:self-start lg:max-h-[calc(100vh-7rem)]">
              <h2 className="text-label font-semibold text-content-primary">
                Plays <span className="tabular-nums text-content-muted">({show.plays.length})</span>
              </h2>
              {show.plays.length === 0 ? (
                <div className="rounded border border-edge bg-surface-raised px-3 py-10 text-center text-body text-content-muted">
                  No plays recorded
                </div>
              ) : (
                <div className="overflow-auto rounded border border-edge bg-surface-raised lg:max-h-[calc(100vh-9rem)]">
                  <table className="w-full text-left text-body">
                    <thead className="sticky top-0 z-10 border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
                      <tr>
                        <th className="px-3 py-2 font-medium">Play</th>
                        <th className="px-3 py-2 font-medium">Status</th>
                        <th className="px-3 py-2 font-medium">Branch</th>
                        <th className="px-3 py-2 font-medium tabular-nums">Exit</th>
                        <th className="px-3 py-2 font-medium">Verdict</th>
                        <th className="px-3 py-2 font-medium tabular-nums text-right">Dur</th>
                        <th className="px-3 py-2 font-medium">Updated</th>
                        <th className="w-8 px-3 py-2"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {show.plays.map((play) => {
                        const flag = reconcileFlag(play.meta);
                        const isExpanded = expanded === play.name;
                        const dur = playDurationSec(play.meta);
                        return (
                          <React.Fragment key={play.name}>
                            <tr
                              className="border-b border-edge-subtle text-content-secondary hover:bg-surface-overlay cursor-pointer"
                              onClick={() => setExpanded(isExpanded ? null : play.name)}
                            >
                              <td className="max-w-[12rem] truncate px-3 py-2 font-mono text-body text-content-primary">
                                {play.name}
                              </td>
                              <td className="px-3 py-2">
                                <StatusPill value={play.meta.status} kind="lifecycle" />
                              </td>
                              <td
                                className="max-w-[10rem] truncate px-3 py-2 font-mono text-meta text-content-muted"
                                title={play.meta.branch}
                              >
                                {middleEllipsis(play.meta.branch, 18)}
                              </td>
                              <td className="px-3 py-2 tabular-nums">
                                <div className="flex flex-wrap items-center gap-1">
                                  <span className="text-body">{play.meta.exit_code ?? "—"}</span>
                                  {flag === "exit_mismatch" && (
                                    <StatusPill
                                      value="exit mismatch"
                                      tone="failed"
                                      kind="verdict"
                                    />
                                  )}
                                  {flag === "missing_exit" && (
                                    <StatusPill
                                      value="missing exit"
                                      tone="pending"
                                      kind="verdict"
                                    />
                                  )}
                                </div>
                              </td>
                              <td className="px-3 py-2">
                                {play.verdict ? (
                                  <StatusPill
                                    kind="verdict"
                                    value={play.verdict.gate_passed ? "passed" : "rejected"}
                                  />
                                ) : (
                                  <span className="text-meta text-content-muted">—</span>
                                )}
                              </td>
                              <td className="px-3 py-2 tabular-nums text-right">
                                <Duration value={dur} />
                              </td>
                              <td className="px-3 py-2 text-meta text-content-muted">
                                <Timestamp
                                  value={
                                    play.updated_at ??
                                    play.meta.ended_at ??
                                    play.meta.started_at ??
                                    null
                                  }
                                />
                              </td>
                              <td className="px-3 py-2">
                                <span
                                  className="text-body text-content-muted"
                                  aria-label={isExpanded ? "Collapse" : "Expand"}
                                >
                                  {isExpanded ? "▴" : "▾"}
                                </span>
                              </td>
                            </tr>

                            {isExpanded && (
                              <tr className="bg-surface-overlay">
                                <td colSpan={8} className="px-4 py-3">
                                  <div className="flex flex-col gap-3">
                                    {/* Intent */}
                                    {play.intent && (
                                      <div>
                                        <div className="text-meta uppercase tracking-[0.06em] text-content-muted">
                                          Intent
                                        </div>
                                        <div className="mt-1">
                                          <Markdown className="text-body">{play.intent}</Markdown>
                                        </div>
                                      </div>
                                    )}

                                    {/* Session link */}
                                    {play.session_id && (
                                      <div>
                                        <div className="text-meta uppercase tracking-[0.06em] text-content-muted">
                                          Session
                                        </div>
                                        <div className="mt-1">
                                          <Link
                                            href={`/runs/${play.session_id}`}
                                            className="inline-flex items-center gap-1 text-body font-medium text-interactive-primary hover:underline"
                                          >
                                            {play.session_name ?? play.session_id}
                                            <span aria-hidden="true">→</span>
                                          </Link>
                                        </div>
                                      </div>
                                    )}

                                    {/* Duration / Exit / Attempt stats row */}
                                    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-body text-content-secondary">
                                      <span>
                                        <span className="text-meta uppercase tracking-[0.06em] text-content-muted mr-1">
                                          Duration
                                        </span>
                                        <Duration value={dur} />
                                      </span>
                                      <span>
                                        <span className="text-meta uppercase tracking-[0.06em] text-content-muted mr-1">
                                          Exit
                                        </span>
                                        <span className="tabular-nums">
                                          {play.meta.exit_code ?? "—"}
                                        </span>
                                      </span>
                                      <span>
                                        <span className="text-meta uppercase tracking-[0.06em] text-content-muted mr-1">
                                          Attempt
                                        </span>
                                        <span className="tabular-nums">{play.meta.attempt}</span>
                                      </span>
                                    </div>

                                    {/* Gate + Feedback */}
                                    {play.verdict && (
                                      <div className="flex flex-col gap-1.5">
                                        <div className="flex items-center gap-2">
                                          <span className="text-meta uppercase tracking-[0.06em] text-content-muted">
                                            Gate
                                          </span>
                                          <StatusPill
                                            kind="verdict"
                                            value={play.verdict.gate_passed ? "passed" : "rejected"}
                                          />
                                        </div>
                                        {play.verdict.feedback && (
                                          <div>
                                            <div className="text-meta uppercase tracking-[0.06em] text-content-muted">
                                              Feedback
                                            </div>
                                            <div className="mt-1">
                                              <Markdown className="text-body">
                                                {play.verdict.feedback}
                                              </Markdown>
                                            </div>
                                          </div>
                                        )}
                                        {play.verdict.notes && (
                                          <div>
                                            <div className="text-meta uppercase tracking-[0.06em] text-content-muted">
                                              Notes
                                            </div>
                                            <div className="mt-1">
                                              <Markdown className="text-body">
                                                {play.verdict.notes}
                                              </Markdown>
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    )}

                                    {/* Raw data (collapsed by default) */}
                                    <div>
                                      <button
                                        type="button"
                                        onClick={() =>
                                          setRawExpanded((prev) => ({
                                            ...prev,
                                            [play.name]: !prev[play.name],
                                          }))
                                        }
                                        className="flex items-center gap-1 text-meta uppercase tracking-[0.06em] text-content-muted hover:text-content-primary transition-colors"
                                      >
                                        <span>{rawExpanded[play.name] ? "▾" : "▸"}</span>
                                        <span>Raw data</span>
                                      </button>
                                      {rawExpanded[play.name] && (
                                        <div className="mt-2 flex flex-col gap-2">
                                          <div>
                                            <div className="flex items-center justify-between text-meta uppercase tracking-[0.06em] text-content-muted">
                                              <span>Meta</span>
                                              <CopyButton
                                                text={JSON.stringify(play.meta, null, 2)}
                                              />
                                            </div>
                                            <pre className="mt-1 overflow-auto rounded border border-edge bg-surface-raised p-2 font-mono text-meta text-content-secondary">
                                              {JSON.stringify(play.meta, null, 2)}
                                            </pre>
                                          </div>
                                          {play.verdict && (
                                            <div>
                                              <div className="flex items-center justify-between text-meta uppercase tracking-[0.06em] text-content-muted">
                                                <span>Verdict</span>
                                                <CopyButton
                                                  text={JSON.stringify(play.verdict, null, 2)}
                                                />
                                              </div>
                                              <pre className="mt-1 overflow-auto rounded border border-edge bg-surface-raised p-2 font-mono text-meta text-content-secondary">
                                                {JSON.stringify(play.verdict, null, 2)}
                                              </pre>
                                            </div>
                                          )}
                                        </div>
                                      )}
                                    </div>
                                  </div>
                                </td>
                              </tr>
                            )}
                          </React.Fragment>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>

          {show.plays.length > 0 && (
            <section className="flex flex-col gap-2">
              <h2 className="text-label font-semibold text-content-primary">Play graph</h2>
              <PlayDag plays={show.plays} showMd={show.show_md} />
            </section>
          )}
        </>
      )}
    </main>
  );
}

function ShowSummaryPanel({
  summary,
  rollup,
}: {
  summary: Record<string, string>;
  rollup: { total: number; failed: Play[]; running: Play[]; merged: Play[] } | null;
}) {
  const hasGoal = Boolean(summary.goal);
  const hasStatus = Boolean(summary.status);
  const hasBlockers = Boolean(summary.blocker);
  const hasNext = Boolean(summary["next action"] || summary["next step"] || summary["next steps"]);
  const nextValue = summary["next action"] ?? summary["next step"] ?? summary["next steps"];

  const showSummary = hasGoal || hasStatus || hasBlockers || hasNext;

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
      {/* Roll-up — always rendered */}
      <section className="rounded border border-edge bg-surface-raised p-3 shadow-card">
        <h3 className="text-meta uppercase tracking-[0.06em] text-content-muted">Roll-up</h3>
        {rollup ? (
          <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-body">
            <SummaryRow
              label="Plays"
              value={<span className="tabular-nums">{rollup.total}</span>}
            />
            <SummaryRow
              label="Running"
              value={
                <span
                  className={
                    rollup.running.length > 0
                      ? "text-status-running tabular-nums"
                      : "text-content-muted tabular-nums"
                  }
                >
                  {rollup.running.length}
                </span>
              }
            />
            <SummaryRow
              label="Failed"
              value={
                <span
                  className={
                    rollup.failed.length > 0
                      ? "text-status-error tabular-nums"
                      : "text-content-muted tabular-nums"
                  }
                >
                  {rollup.failed.length}
                </span>
              }
            />
            <SummaryRow
              label="Merged"
              value={
                <span className="tabular-nums text-content-secondary">{rollup.merged.length}</span>
              }
            />
          </dl>
        ) : (
          <p className="mt-2 text-body text-content-muted">No plays yet.</p>
        )}
      </section>

      {/* Goal / Status */}
      <section className="rounded border border-edge bg-surface-raised p-3 shadow-card">
        <h3 className="text-meta uppercase tracking-[0.06em] text-content-muted">Plan</h3>
        {showSummary ? (
          <dl className="mt-2 flex flex-col gap-1.5 text-body">
            {hasGoal && <SummaryBlock label="Goal" value={summary.goal} />}
            {hasStatus && <SummaryBlock label="Status" value={summary.status} />}
          </dl>
        ) : (
          <p className="mt-2 text-body text-content-muted">
            Add{" "}
            <code className="rounded bg-surface-overlay px-1 text-content-secondary">Goal:</code> /{" "}
            <code className="rounded bg-surface-overlay px-1 text-content-secondary">Status:</code>{" "}
            lines near the top of <code className="text-content-secondary">_show.md</code> to
            surface them here.
          </p>
        )}
      </section>

      {/* Blockers / Next action */}
      <section className="rounded border border-edge bg-surface-raised p-3 shadow-card">
        <h3 className="text-meta uppercase tracking-[0.06em] text-content-muted">Action</h3>
        {hasBlockers || hasNext ? (
          <dl className="mt-2 flex flex-col gap-1.5 text-body">
            {hasBlockers && <SummaryBlock label="Blockers" value={summary.blocker} tone="failed" />}
            {hasNext && nextValue && <SummaryBlock label="Next" value={nextValue} />}
          </dl>
        ) : (
          <p className="mt-2 text-body text-content-muted">
            No blockers or next action declared in plan.
          </p>
        )}
      </section>
    </div>
  );
}

function SummaryRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <>
      <dt className="text-meta uppercase tracking-[0.06em] text-content-muted">{label}</dt>
      <dd className="text-right text-body text-content-primary">{value}</dd>
    </>
  );
}

function SummaryBlock({ label, value, tone }: { label: string; value: string; tone?: "failed" }) {
  return (
    <div>
      <dt className="text-meta uppercase tracking-[0.06em] text-content-muted">{label}</dt>
      <dd
        className={`text-body ${tone === "failed" ? "text-status-error" : "text-content-primary"}`}
      >
        {value}
      </dd>
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(() => {
    try {
      void navigator.clipboard.writeText(text).then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      });
    } catch {
      /* clipboard unavailable */
    }
  }, [text]);

  return (
    <button
      type="button"
      onClick={handleCopy}
      className="rounded border border-edge bg-surface-raised px-2 py-0.5 font-mono text-meta text-content-muted hover:border-edge-strong hover:text-content-primary"
    >
      {copied ? "copied" : "copy"}
    </button>
  );
}

function middleEllipsis(s: string | undefined, maxLen: number): string {
  if (!s) return "";
  if (s.length <= maxLen) return s;
  const head = Math.ceil((maxLen - 1) / 2);
  const tail = Math.floor((maxLen - 1) / 2);
  return `${s.slice(0, head)}…${s.slice(s.length - tail)}`;
}
