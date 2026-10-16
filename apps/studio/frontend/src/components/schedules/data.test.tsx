import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { IntlProvider } from "use-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { listScheduleRuns, listSchedules } from "@/lib/api";
import type { ScheduleRunSliceRow, ScheduleSummary } from "@/lib/types";
import en from "@/messages/en.json";
import ScheduleCards from "./ScheduleCards";
import SchedulesTable from "./SchedulesTable";
import { recentRunsUnavailable, useSchedulesData, type SchedulesData } from "./data";

vi.mock("@/lib/api", () => ({
  listSchedules: vi.fn(),
  listScheduleRuns: vi.fn(),
  triggerSchedule: vi.fn(),
  updateSchedule: vi.fn(),
}));
vi.mock("@/components/ui/Toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));

function run(id: string, index = 0): ScheduleRunSliceRow {
  return {
    id: `${id}-run-${index}`,
    schedule_id: id,
    invocation_id: null,
    action_kind: "agent",
    status: "completed",
    exit_code: 0,
    chain_depth: 0,
    fired_at: 100 - index,
    ended_at: 101 - index,
    error_class: null,
  };
}

function schedule(id: string, runs: ScheduleRunSliceRow[] = []): ScheduleSummary {
  return {
    id,
    name: id,
    description: null,
    enabled: 1,
    trigger_type: "interval",
    cron_expr: null,
    interval_sec: 60,
    github_repo: null,
    poll_interval_sec: null,
    action_kind: "agent",
    action_model: null,
    action_agent: null,
    action_playbook: null,
    action_project: null,
    last_fired_at: null,
    next_fire_at: null,
    missed_fire_policy: "skip",
    overlap_policy: "skip",
    project: null,
    created_at: 1,
    updated_at: 1,
    recent_runs_summary: { version: 1, limit: 25, status: "available", has_next: false, runs },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

describe("schedule summary refresh", () => {
  let container: HTMLDivElement;
  let root: Root;
  let latest: SchedulesData;
  let unmounted: boolean;

  function Harness() {
    latest = useSchedulesData();
    return null;
  }

  beforeEach(() => {
    vi.resetAllMocks();
    vi.useFakeTimers();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    unmounted = false;
  });

  afterEach(() => {
    if (!unmounted) act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });

  it("loads 100 definitions and 2500 recent rows in one request, without history calls", async () => {
    const rows = Array.from({ length: 100 }, (_, index) => {
      const id = `schedule-${index}`;
      return schedule(
        id,
        Array.from({ length: 25 }, (_, runIndex) => run(id, runIndex)),
      );
    });
    vi.mocked(listSchedules).mockResolvedValue({ schedules: rows });
    await act(async () => root.render(<Harness />));
    expect(listSchedules).toHaveBeenCalledTimes(1);
    expect(listSchedules).toHaveBeenCalledWith({ include_recent_runs: true });
    expect(listScheduleRuns).not.toHaveBeenCalled();
    expect(latest.schedules).toHaveLength(100);
    expect(latest.runs).toHaveLength(2500);
    expect(latest.runs[0].scheduleName).toBe("schedule-0");
    expect(latest.runs.slice(0, 25).map((row) => row.id)).toEqual(
      rows[0].recent_runs_summary!.runs.map((row) => row.id),
    );
    expect(latest.error).toBe(false);
  });

  it("preserves good and empty slices while attributing failed and unknown versions", async () => {
    const empty = schedule("empty");
    const good = schedule("good", [run("good")]);
    const failed = schedule("failed");
    failed.recent_runs_summary = {
      version: 1,
      limit: 25,
      status: "unavailable",
      has_next: null,
      runs: [],
    };
    const unknown = schedule("unknown");
    unknown.recent_runs_summary = {
      ...unknown.recent_runs_summary!,
      version: 2,
    } as unknown as ScheduleSummary["recent_runs_summary"];
    vi.mocked(listSchedules).mockResolvedValue({
      schedules: [empty, good, failed, unknown],
    });
    await act(async () => root.render(<Harness />));
    expect(latest.runs.map((row) => row.id)).toEqual(["good-run-0"]);
    expect(latest.schedules.filter(recentRunsUnavailable).map((row) => row.id)).toEqual([
      "failed",
      "unknown",
    ]);
    expect(latest.schedules[0].recent_runs_summary?.status).toBe("available");
    // A slice the daemon reports as unavailable is not a version gap, so it
    // does not send a per-schedule history read.
    expect(listScheduleRuns).not.toHaveBeenCalled();
  });

  it("falls back to the per-schedule history read for a daemon that omits the field", async () => {
    const legacy = schedule("legacy");
    delete legacy.recent_runs_summary;
    vi.mocked(listSchedules).mockResolvedValue({ schedules: [legacy] });
    vi.mocked(listScheduleRuns).mockResolvedValue({ runs: [run("legacy")], has_next: false });
    await act(async () => root.render(<Harness />));
    expect(listScheduleRuns).toHaveBeenCalledWith("legacy", { limit: 25 });
    expect(recentRunsUnavailable(latest.schedules[0])).toBe(false);
    expect(latest.runs.map((row) => row.id)).toEqual(["legacy-run-0"]);
  });

  it("falls back only for the rows a daemon answered without the field", async () => {
    const modern = schedule("modern", [run("modern")]);
    const legacy = schedule("legacy");
    delete legacy.recent_runs_summary;
    vi.mocked(listSchedules).mockResolvedValue({ schedules: [modern, legacy] });
    vi.mocked(listScheduleRuns).mockResolvedValue({ runs: [run("legacy")], has_next: false });
    await act(async () => root.render(<Harness />));
    expect(listScheduleRuns).toHaveBeenCalledTimes(1);
    expect(listScheduleRuns).toHaveBeenCalledWith("legacy", { limit: 25 });
    expect(latest.runs.map((row) => row.id).sort()).toEqual(["legacy-run-0", "modern-run-0"]);
  });

  it("keeps a schedule whose fallback read failed unavailable", async () => {
    const legacy = schedule("legacy");
    delete legacy.recent_runs_summary;
    vi.mocked(listSchedules).mockResolvedValue({ schedules: [legacy] });
    vi.mocked(listScheduleRuns).mockRejectedValue(new Error("Unavailable"));
    await act(async () => root.render(<Harness />));
    expect(recentRunsUnavailable(latest.schedules[0])).toBe(true);
    expect(latest.runs).toEqual([]);
  });

  it("rejects a cross-schedule slice instead of attaching its runs to another definition", async () => {
    vi.mocked(listSchedules).mockResolvedValue({ schedules: [schedule("one", [run("other")])] });
    await act(async () => root.render(<Harness />));
    expect(latest.runs).toEqual([]);
    expect(recentRunsUnavailable(latest.schedules[0])).toBe(true);
  });

  it("coalesces explicit refreshes and polls during a pending request into one trailing request", async () => {
    const first = deferred<Awaited<ReturnType<typeof listSchedules>>>();
    const trailing = deferred<Awaited<ReturnType<typeof listSchedules>>>();
    vi.mocked(listSchedules)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(trailing.promise);
    await act(async () => root.render(<Harness />));
    await act(async () => {
      for (let index = 0; index < 100; index++) latest.refresh();
      vi.advanceTimersByTime(90_000);
    });
    expect(listSchedules).toHaveBeenCalledTimes(1);
    await act(async () => first.resolve({ schedules: [schedule("first")] }));
    expect(listSchedules).toHaveBeenCalledTimes(2);
    await act(async () => trailing.resolve({ schedules: [schedule("trailing")] }));
    expect(listSchedules).toHaveBeenCalledTimes(2);
    expect(latest.schedules[0].id).toBe("trailing");
  });

  it("retains previous data on transport failure and permits the queued refresh to recover", async () => {
    vi.mocked(listSchedules).mockResolvedValueOnce({
      schedules: [schedule("existing", [run("existing")])],
    });
    await act(async () => root.render(<Harness />));
    const failed = deferred<Awaited<ReturnType<typeof listSchedules>>>();
    const recovery = deferred<Awaited<ReturnType<typeof listSchedules>>>();
    vi.mocked(listSchedules)
      .mockReturnValueOnce(failed.promise)
      .mockReturnValueOnce(recovery.promise);
    await act(async () => {
      latest.refresh();
      latest.refresh();
    });
    await act(async () => failed.reject(new Error("Unavailable")));
    expect(latest.error).toBe(true);
    expect(latest.runs).toHaveLength(1);
    expect(listSchedules).toHaveBeenCalledTimes(3);
    await act(async () => recovery.resolve({ schedules: [] }));
    expect(latest.error).toBe(false);
    expect(latest.schedules).toEqual([]);
  });

  it("does not launch a queued refresh after unmount", async () => {
    const pending = deferred<Awaited<ReturnType<typeof listSchedules>>>();
    vi.mocked(listSchedules).mockReturnValue(pending.promise);
    await act(async () => root.render(<Harness />));
    act(() => {
      latest.refresh();
      root.unmount();
    });
    unmounted = true;
    await act(async () => pending.resolve({ schedules: [schedule("late")] }));
    expect(listSchedules).toHaveBeenCalledTimes(1);
    expect(latest.schedules).toEqual([]);
  });

  it("ignores a response from a cleaned-up effect when StrictMode remounts", async () => {
    const old = deferred<Awaited<ReturnType<typeof listSchedules>>>();
    vi.mocked(listSchedules)
      .mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce({ schedules: [schedule("current")] });
    await act(async () =>
      root.render(
        <StrictMode>
          <Harness />
        </StrictMode>,
      ),
    );
    await act(async () => old.resolve({ schedules: [schedule("old")] }));
    expect(latest.schedules[0].id).toBe("current");
  });
});

describe.each([ScheduleCards, SchedulesTable])("schedule recent-run presentation", (Component) => {
  it("labels unavailable slices while preserving the genuine never-run state", () => {
    const missing = schedule("unavailable definition");
    delete missing.recent_runs_summary;
    const html = renderToStaticMarkup(
      <IntlProvider locale="en" messages={en}>
        <Component
          schedules={[missing, schedule("empty definition")]}
          runs={[]}
          nowMs={100_000}
          onChanged={() => {}}
          onOpen={() => {}}
        />
      </IntlProvider>,
    );
    expect(html.match(/Recent runs unavailable/g)).toHaveLength(1);
    expect(html.match(/>Never</g)).toHaveLength(1);
    expect(html).toContain("unavailable definition");
    expect(html).toContain("empty definition");
  });
});
