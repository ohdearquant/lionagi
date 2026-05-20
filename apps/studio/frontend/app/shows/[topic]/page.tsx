"use client";

import Link from "next/link";
import React, { use, useCallback, useEffect, useState } from "react";
import Badge from "@/components/Badge";
import Markdown from "@/components/Markdown";
import { getShow, streamShow } from "@/lib/api";
import type { PlayMeta, ShowDetail, ShowEvent } from "@/lib/types";
import PlayDag from "./components/PlayDag";

type Play = ShowDetail["plays"][number];

function formatTime(ts: string | number | null | undefined): string {
  if (!ts) return "—";
  if (typeof ts === "number") return new Date(ts * 1000).toLocaleString();
  return new Date(ts).toLocaleString();
}

function playTone(play: Play): "ok" | "failed" | "pending" | "default" {
  if (play.verdict?.gate_passed === true) return "ok";
  if (play.verdict?.gate_passed === false) return "failed";
  if (play.meta.status === "running" || play.meta.status === "pending") return "pending";
  if (play.meta.status === "merged") return "ok";
  return "default";
}

function reconcileFlag(meta: PlayMeta): "exit_mismatch" | "missing_exit" | null {
  if (typeof meta.exit_code === "number" && meta.exit_code !== 0) return "exit_mismatch";
  if (meta.exit_code === undefined && Boolean(meta.merged_at)) return "missing_exit";
  return null;
}

export default function ShowDetailPage({ params }: { params: Promise<{ topic: string }> }) {
  const { topic: rawTopic } = use(params);
  const topic = decodeURIComponent(rawTopic);
  const [show, setShow] = useState<ShowDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await getShow(topic);
      setShow(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load show");
    }
  }, [topic]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!live) return;
    return streamShow(topic, (_event: ShowEvent) => {
      void load();
    });
  }, [live, load, topic]);

  const latestPlay = show?.plays.at(-1);
  const latestStatus = latestPlay?.meta.status ?? "—";

  return (
    <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-4 px-4 py-6 text-content-primary">
      <header className="flex flex-col gap-3 border-b border-edge pb-4">
        <Link href="/shows" className="text-body text-content-muted hover:text-content-primary">
          / shows
        </Link>

        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <h1 className="font-mono text-xl font-semibold">{topic}</h1>
            <Badge value={latestStatus}>{latestStatus}</Badge>
          </div>
          <button
            onClick={() => setLive((v) => !v)}
            className={[
              "rounded border px-3 py-1 text-body font-medium transition-colors",
              live
                ? "border-status-success/40 bg-status-success-bg text-status-success"
                : "border-edge bg-surface-raised text-content-muted hover:text-content-primary",
            ].join(" ")}
          >
            {live ? "Live: ON" : "Live: OFF"}
          </button>
        </div>
      </header>

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
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* Left: _show.md */}
            <section className="flex flex-col gap-2">
              <h2 className="text-label font-semibold text-content-primary">_show.md</h2>
              <div className="overflow-auto rounded border border-edge bg-surface-raised p-4">
                {show.show_md ? (
                  <Markdown>{show.show_md}</Markdown>
                ) : (
                  <p className="text-body text-content-muted">No _show.md found.</p>
                )}
              </div>
            </section>

            {/* Right: plays table */}
            <section className="flex flex-col gap-2">
              <h2 className="text-label font-semibold text-content-primary">
                Plays ({show.plays.length})
              </h2>
              {show.plays.length === 0 ? (
                <div className="rounded border border-edge bg-surface-raised px-3 py-10 text-center text-body text-content-muted">
                  No plays recorded
                </div>
              ) : (
                <div className="overflow-x-auto rounded border border-edge bg-surface-raised">
                  <table className="w-full text-left text-body">
                    <thead className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
                      <tr>
                        <th className="px-3 py-2 font-medium">Play</th>
                        <th className="px-3 py-2 font-medium">Status</th>
                        <th className="px-3 py-2 font-medium">Branch</th>
                        <th className="px-3 py-2 font-medium">Exit</th>
                        <th className="px-3 py-2 font-medium">Verdict</th>
                        <th className="px-3 py-2 font-medium">Updated</th>
                        <th className="px-3 py-2 w-8"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {show.plays.map((play) => {
                        const flag = reconcileFlag(play.meta);
                        const isExpanded = expanded === play.name;
                        return (
                          <React.Fragment key={play.name}>
                            <tr className="border-b border-edge-subtle text-content-secondary hover:bg-surface-overlay">
                              <td className="max-w-[12rem] truncate px-3 py-2 font-mono text-body">
                                {play.name}
                              </td>
                              <td className="px-3 py-2">
                                <Badge tone={playTone(play)}>{play.meta.status}</Badge>
                              </td>
                              <td className="max-w-[10rem] truncate px-3 py-2 font-mono text-meta text-content-muted">
                                {play.meta.branch}
                              </td>
                              <td className="px-3 py-2">
                                <div className="flex flex-wrap items-center gap-1">
                                  <span className="text-body">{play.meta.exit_code ?? "—"}</span>
                                  {flag === "exit_mismatch" && (
                                    <Badge tone="failed">exit mismatch</Badge>
                                  )}
                                  {flag === "missing_exit" && (
                                    <Badge tone="pending">missing exit</Badge>
                                  )}
                                </div>
                              </td>
                              <td className="px-3 py-2">
                                {play.verdict ? (
                                  <Badge tone={play.verdict.gate_passed ? "ok" : "failed"}>
                                    {play.verdict.gate_passed ? "passed" : "failed"}
                                  </Badge>
                                ) : (
                                  <span className="text-meta text-content-muted">—</span>
                                )}
                              </td>
                              <td className="px-3 py-2 text-meta text-content-muted">
                                {formatTime(
                                  play.updated_at ??
                                    play.meta.ended_at ??
                                    play.meta.started_at,
                                )}
                              </td>
                              <td className="px-3 py-2">
                                <button
                                  onClick={() =>
                                    setExpanded(isExpanded ? null : play.name)
                                  }
                                  className="text-body text-content-muted hover:text-content-primary"
                                  aria-label={isExpanded ? "Collapse" : "Expand"}
                                >
                                  {isExpanded ? "▴" : "▾"}
                                </button>
                              </td>
                            </tr>

                            {isExpanded && (
                              <tr className="bg-surface-overlay">
                                <td colSpan={7} className="px-4 py-3">
                                  <div className="flex flex-col gap-3">
                                    {play.verdict?.feedback && (
                                      <div>
                                        <div className="text-meta uppercase tracking-[0.06em] text-content-muted">
                                          Feedback
                                        </div>
                                        <p className="mt-1 whitespace-pre-wrap text-body text-content-secondary">
                                          {play.verdict.feedback}
                                        </p>
                                      </div>
                                    )}
                                    {play.verdict?.notes && (
                                      <div>
                                        <div className="text-meta uppercase tracking-[0.06em] text-content-muted">
                                          Notes
                                        </div>
                                        <p className="mt-1 whitespace-pre-wrap text-body text-content-secondary">
                                          {play.verdict.notes}
                                        </p>
                                      </div>
                                    )}
                                    <div>
                                      <div className="text-meta uppercase tracking-[0.06em] text-content-muted">
                                        Meta
                                      </div>
                                      <pre className="mt-1 overflow-auto rounded border border-edge bg-surface-raised p-2 font-mono text-meta text-content-secondary">
                                        {JSON.stringify(play.meta, null, 2)}
                                      </pre>
                                    </div>
                                    {play.verdict && (
                                      <div>
                                        <div className="text-meta uppercase tracking-[0.06em] text-content-muted">
                                          Verdict
                                        </div>
                                        <pre className="mt-1 overflow-auto rounded border border-edge bg-surface-raised p-2 font-mono text-meta text-content-secondary">
                                          {JSON.stringify(play.verdict, null, 2)}
                                        </pre>
                                      </div>
                                    )}
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
              <h2 className="text-label font-semibold text-content-primary">Play Graph</h2>
              <PlayDag plays={show.plays} showMd={show.show_md} />
            </section>
          )}
        </>
      )}
    </main>
  );
}
