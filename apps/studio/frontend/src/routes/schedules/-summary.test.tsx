import { act, type ComponentType } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSchedulesData } from "@/components/schedules/data";
import type { ScheduleSummary } from "@/lib/types";
import en from "@/messages/en.json";
import { Route } from "./index";

vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof import("@tanstack/react-router")>()),
  createFileRoute: () => (options: object) => ({ ...options, useSearch: () => ({}) }),
  useNavigate: () => vi.fn(),
}));
vi.mock("@/components/schedules/data", async (original) => ({
  ...(await original<typeof import("@/components/schedules/data")>()),
  useSchedulesData: vi.fn(),
}));
vi.mock("@/components/schedules/ScheduleCards", () => ({
  default: () => <div>Schedule cards</div>,
}));
vi.mock("@/components/schedules/SchedulesTable", () => ({
  default: () => <div>Schedule table</div>,
}));
vi.mock("@/components/schedules/SchedulesCalendar", () => ({
  default: () => <div>Schedule calendar</div>,
}));
vi.mock("@/components/schedules/ScheduleDetailModal", () => ({ default: () => null }));
vi.mock("@/components/schedules/CreateScheduleModal", () => ({ default: () => null }));

const Page = (Route as unknown as { component: ComponentType }).component;
let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  await (Page as ComponentType & { preload?: () => Promise<void> }).preload?.();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

function renderPage(schedules: ScheduleSummary[], error = false) {
  const refresh = vi.fn();
  vi.mocked(useSchedulesData).mockReturnValue({
    schedules,
    runs: [],
    nowMs: 100,
    loading: false,
    error,
    refresh,
  });
  act(() =>
    root.render(
      <IntlProvider locale="en" messages={en}>
        <Page />
      </IntlProvider>,
    ),
  );
  return refresh;
}

it("attributes missing slices in calendar mode without hiding available definitions and retries the batch", () => {
  const refresh = renderPage([
    { id: "missing", name: "Nightly" } as ScheduleSummary,
    {
      id: "empty",
      name: "Hourly",
      recent_runs_summary: {
        version: 1,
        limit: 25,
        status: "available",
        has_next: false,
        runs: [],
      },
    } as unknown as ScheduleSummary,
  ]);
  const calendar = Array.from(container.querySelectorAll("button")).find(
    (button) => button.textContent === en.schedules.calendar,
  )!;
  act(() => calendar.click());
  expect(container.textContent).toContain("Schedule calendar");
  expect(container.querySelector('[role="alert"]')?.textContent).toContain(
    "Recent runs unavailable: Nightly",
  );
  expect(container.querySelector('[role="alert"]')?.textContent).not.toContain("Hourly");
  const retry = container.querySelector<HTMLButtonElement>('[role="alert"] button')!;
  act(() => retry.click());
  expect(refresh).toHaveBeenCalledTimes(1);
});

it("does not render an empty-schedules claim when the first batch failed", () => {
  renderPage([], true);
  expect(container.querySelector('[role="alert"]')?.textContent).toContain(en.schedules.loadError);
  expect(container.textContent).not.toContain(en.schedules.emptyTitle);
});
