"use client";

import Link from "next/link";
import React, { useCallback, useEffect, useState } from "react";
import Badge from "@/components/Badge";
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

export default function ShowDetailPage({ params }: { params: { topic: string } }) {
  const topic = decodeURIComponent(params.topic);
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
    <main className="mx-auto flex w-full max-w-[1600px] flex-col gap-4 px-4 py-6 text-neutral-200">
      <header className="flex flex-col gap-3 border-b border-neutral-800 pb-4">
        <Link href="/shows" className="text-sm text-neutral-500 hover:text-neutral-200">
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
              "rounded border px-3 py-1 text-sm font-medium transition",
              live
                ? "border-emerald-700 bg-emerald-900/50 text-emerald-300"
                : "border-neutral-700 bg-neutral-900 text-neutral-400 hover:text-neutral-200",
            ].join(" ")}
          >
            {live ? "Live: ON" : "Live: OFF"}
          </button>
        </div>
      </header>

      {error && (
        <div className="border border-red-800 bg-neutral-950 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {!show && !error && (
        <div className="py-10 text-center text-sm text-neutral-500">Loading...</div>
      )}

      {show && (
        <>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* Left: _show.md */}
            <section className="flex flex-col gap-2">
              <h2 className="text-sm font-semibold text-neutral-200">_show.md</h2>
              <div className="overflow-auto rounded border border-neutral-800 bg-neutral-950 p-3">
                <pre className="whitespace-pre-wrap break-words text-sm leading-6 text-neutral-400">
                  {show.show_md ?? "No _show.md found."}
                </pre>
              </div>
            </section>

            {/* Right: plays table */}
            <section className="flex flex-col gap-2">
              <h2 className="text-sm font-semibold text-neutral-200">
                Plays ({show.plays.length})
              </h2>
              {show.plays.length === 0 ? (
                <div className="border border-neutral-800 bg-neutral-950 px-3 py-10 text-center text-sm text-neutral-500">
                  No plays recorded
                </div>
              ) : (
                <div className="overflow-x-auto border border-neutral-800">
                  <table className="w-full text-left text-sm">
                    <thead className="border-b border-neutral-800 bg-neutral-900/70 text-xs uppercase text-neutral-500">
                      <tr>
                        <th className="px-3 py-2">Play</th>
                        <th className="px-3 py-2">Status</th>
                        <th className="px-3 py-2">Branch</th>
                        <th className="px-3 py-2">Exit</th>
                        <th className="px-3 py-2">Verdict</th>
                        <th className="px-3 py-2">Updated</th>
                        <th className="px-3 py-2 w-8"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {show.plays.map((play) => {
                        const flag = reconcileFlag(play.meta);
                        const isExpanded = expanded === play.name;
                        return (
                          <React.Fragment key={play.name}>
                            <tr className="border-b border-neutral-900 text-neutral-300 hover:bg-neutral-900/50">
                              <td className="max-w-[12rem] truncate px-3 py-2 font-mono text-xs">
                                {play.name}
                              </td>
                              <td className="px-3 py-2">
                                <Badge tone={playTone(play)}>{play.meta.status}</Badge>
                              </td>
                              <td className="max-w-[10rem] truncate px-3 py-2 font-mono text-xs text-neutral-400">
                                {play.meta.branch}
                              </td>
                              <td className="px-3 py-2">
                                <div className="flex flex-wrap items-center gap-1">
                                  <span className="text-xs">{play.meta.exit_code ?? "—"}</span>
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
                                  <span className="text-xs text-neutral-600">—</span>
                                )}
                              </td>
                              <td className="px-3 py-2 text-xs text-neutral-500">
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
                                  className="text-xs text-neutral-500 hover:text-neutral-200"
                                >
                                  {isExpanded ? "▴" : "▾"}
                                </button>
                              </td>
                            </tr>

                            {isExpanded && (
                              <tr className="bg-neutral-950">
                                <td colSpan={7} className="px-4 py-3">
                                  <div className="flex flex-col gap-3">
                                    {play.verdict?.feedback && (
                                      <div>
                                        <div className="text-xs uppercase text-neutral-500">
                                          Feedback
                                        </div>
                                        <p className="mt-1 whitespace-pre-wrap text-sm text-neutral-300">
                                          {play.verdict.feedback}
                                        </p>
                                      </div>
                                    )}
                                    {play.verdict?.notes && (
                                      <div>
                                        <div className="text-xs uppercase text-neutral-500">
                                          Notes
                                        </div>
                                        <p className="mt-1 whitespace-pre-wrap text-sm text-neutral-300">
                                          {play.verdict.notes}
                                        </p>
                                      </div>
                                    )}
                                    <div>
                                      <div className="text-xs uppercase text-neutral-500">
                                        Meta
                                      </div>
                                      <pre className="mt-1 overflow-auto rounded border border-neutral-800 bg-neutral-900 p-2 font-mono text-xs text-neutral-400">
                                        {JSON.stringify(play.meta, null, 2)}
                                      </pre>
                                    </div>
                                    {play.verdict && (
                                      <div>
                                        <div className="text-xs uppercase text-neutral-500">
                                          Verdict
                                        </div>
                                        <pre className="mt-1 overflow-auto rounded border border-neutral-800 bg-neutral-900 p-2 font-mono text-xs text-neutral-400">
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
              <h2 className="text-sm font-semibold text-neutral-200">Play Graph</h2>
              <PlayDag plays={show.plays} showMd={show.show_md} />
            </section>
          )}
        </>
      )}
    </main>
  );
}
