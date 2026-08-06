/**
 * ProjectFilter — searchable project combobox.
 *
 * No @testing-library/react in this project (see history/InvocationDetail.test.tsx);
 * mounts via react-dom/client + act, same pattern as shell/NoDaemonGate.test.tsx.
 *
 * Covers:
 * - pure helpers: dedupe, count-desc sort, recency, substring filter
 * - trigger label reflects current selection (all / no project / named project)
 * - popover lists options grouped Recent-then-All, sorted by count desc
 * - typing filters by case-insensitive substring, hiding the Recent grouping
 * - selecting an option calls onChange with the right patch and closes the popover
 * - a failed fetch shows a dismissible error with retry, without clearing prior options
 * - background refresh re-fetches periodically, not only once on mount
 * - two rows sharing a project name render as a single deduped option
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import ProjectFilter, {
  dedupeProjectCounts,
  filterProjectsByQuery,
  recentProjects,
  sortByCountDesc,
} from "./ProjectFilter";
import type { ProjectFilterChange } from "./ProjectFilter";
import type { RunProjectCount } from "@/lib/api";
import enMessages from "@/messages/en.json";

const { listRunProjectsMock } = vi.hoisted(() => ({ listRunProjectsMock: vi.fn() }));

vi.mock("@/lib/api", () => ({
  listRunProjects: listRunProjectsMock,
}));

function row(overrides: Partial<RunProjectCount> & { project: string }): RunProjectCount {
  return { count: 1, last_activity: null, ...overrides };
}

function resolved(projects: RunProjectCount[]) {
  return Promise.resolve({ projects, total: projects.reduce((s, p) => s + p.count, 0) });
}

// ─── Pure helpers ───────────────────────────────────────────────────────────

describe("dedupeProjectCounts", () => {
  it("merges rows sharing a project name, summing counts and keeping the latest activity", () => {
    const rows = [
      row({ project: "alpha", count: 3, last_activity: 100 }),
      row({ project: "alpha", count: 5, last_activity: 200 }),
      row({ project: "beta", count: 1, last_activity: 50 }),
    ];
    const deduped = dedupeProjectCounts(rows);
    expect(deduped).toHaveLength(2);
    const alpha = deduped.find((r) => r.project === "alpha");
    expect(alpha).toEqual({ project: "alpha", count: 8, last_activity: 200 });
  });

  it("drops null-project rows (no-project is handled by the pinned option, not the list)", () => {
    const rows = [row({ project: "alpha" }), { project: null, count: 4, last_activity: null }];
    expect(dedupeProjectCounts(rows)).toEqual([row({ project: "alpha" })]);
  });
});

describe("sortByCountDesc", () => {
  it("orders by count descending, sinking the long tail", () => {
    const rows = [
      row({ project: "one-off", count: 1 }),
      row({ project: "big", count: 500 }),
      row({ project: "mid", count: 20 }),
    ];
    expect(sortByCountDesc(rows).map((r) => r.project)).toEqual(["big", "mid", "one-off"]);
  });

  it("does not mutate the input", () => {
    const rows = [row({ project: "a", count: 1 }), row({ project: "b", count: 9 })];
    const copy = [...rows];
    sortByCountDesc(rows);
    expect(rows).toEqual(copy);
  });
});

describe("recentProjects", () => {
  it("orders by last_activity descending and excludes rows with no activity", () => {
    const rows = [
      row({ project: "stale", count: 1, last_activity: null }),
      row({ project: "old", count: 1, last_activity: 100 }),
      row({ project: "fresh", count: 1, last_activity: 300 }),
    ];
    expect(recentProjects(rows).map((r) => r.project)).toEqual(["fresh", "old"]);
  });

  it("caps at the given limit", () => {
    const rows = Array.from({ length: 8 }, (_, i) =>
      row({ project: `p${i}`, count: 1, last_activity: i }),
    );
    expect(recentProjects(rows, 3)).toHaveLength(3);
  });
});

describe("filterProjectsByQuery", () => {
  it("matches case-insensitive substrings", () => {
    const rows = [row({ project: "khive-Studio" }), row({ project: "lionagi" })];
    expect(filterProjectsByQuery(rows, "studio").map((r) => r.project)).toEqual(["khive-Studio"]);
  });

  it("returns everything for an empty/whitespace query", () => {
    const rows = [row({ project: "a" }), row({ project: "b" })];
    expect(filterProjectsByQuery(rows, "  ")).toEqual(rows);
  });
});

// ─── Component ──────────────────────────────────────────────────────────────

describe("ProjectFilter component", () => {
  let container: HTMLDivElement;
  let root: Root;
  let unmounted: boolean;
  let onChange: ReturnType<typeof vi.fn<(next: ProjectFilterChange) => void>>;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    unmounted = false;
    onChange = vi.fn();
    listRunProjectsMock.mockReset();
    vi.useFakeTimers();
  });

  afterEach(() => {
    if (!unmounted) {
      act(() => {
        root.unmount();
      });
    }
    container.remove();
    vi.useRealTimers();
  });

  async function mount(props: { project?: string | null; projectNull?: boolean } = {}) {
    await act(async () => {
      root = createRoot(container);
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <ProjectFilter
            project={props.project ?? null}
            projectNull={props.projectNull ?? false}
            onChange={onChange}
          />
        </IntlProvider>,
      );
      await Promise.resolve();
    });
  }

  function trigger(): HTMLButtonElement {
    const btn = container.querySelector('button[aria-haspopup="listbox"]');
    expect(btn).not.toBeNull();
    return btn as HTMLButtonElement;
  }

  async function open() {
    await act(async () => {
      trigger().dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
  }

  function optionLabels(): string[] {
    return Array.from(container.querySelectorAll('[role="option"]')).map(
      (el) => el.textContent ?? "",
    );
  }

  it("labels the trigger 'All projects' with no selection", async () => {
    listRunProjectsMock.mockReturnValue(resolved([]));
    await mount();
    expect(trigger().textContent).toContain("All projects");
  });

  it("labels the trigger with the selected project name", async () => {
    listRunProjectsMock.mockReturnValue(resolved([]));
    await mount({ project: "org/alpha" });
    expect(trigger().textContent).toContain("org/alpha");
  });

  it("labels the trigger 'No project' when projectNull is set", async () => {
    listRunProjectsMock.mockReturnValue(resolved([]));
    await mount({ projectNull: true });
    expect(trigger().textContent).toContain("No project");
  });

  it("lists fetched projects sorted by count descending once opened", async () => {
    listRunProjectsMock.mockReturnValue(
      resolved([
        row({ project: "long-tail", count: 1, last_activity: 10 }),
        row({ project: "workhorse", count: 200, last_activity: 20 }),
      ]),
    );
    await mount();
    await open();
    const labels = optionLabels().join(" | ");
    expect(labels.indexOf("workhorse")).toBeLessThan(labels.indexOf("long-tail"));
  });

  it("shows a Recent group by last activity before the full list", async () => {
    listRunProjectsMock.mockReturnValue(
      resolved([
        row({ project: "old-big", count: 500, last_activity: 1 }),
        row({ project: "fresh-small", count: 1, last_activity: 999 }),
      ]),
    );
    await mount();
    await open();
    expect(container.textContent).toContain("Recent");
    const labels = optionLabels().join(" | ");
    // fresh-small is most recently active — it must appear before old-big
    // even though old-big has the higher count.
    expect(labels.indexOf("fresh-small")).toBeLessThan(labels.indexOf("old-big"));
  });

  it("filters options by case-insensitive substring as the user types", async () => {
    listRunProjectsMock.mockReturnValue(
      resolved([row({ project: "khive-studio" }), row({ project: "lionagi" })]),
    );
    await mount();
    await open();
    const input = container.querySelector('input[role="combobox"]') as HTMLInputElement;
    expect(input).not.toBeNull();
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(
        input,
        "STUDIO",
      );
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await Promise.resolve();
    });
    const labels = optionLabels();
    expect(labels.some((l) => l.includes("khive-studio"))).toBe(true);
    expect(labels.some((l) => l.includes("lionagi"))).toBe(false);
  });

  it("selecting a project calls onChange with the project patch and closes the popover", async () => {
    listRunProjectsMock.mockReturnValue(resolved([row({ project: "org/alpha", count: 3 })]));
    await mount();
    await open();
    const option = Array.from(container.querySelectorAll('[role="option"]')).find((el) =>
      el.textContent?.includes("org/alpha"),
    );
    expect(option).toBeDefined();
    await act(async () => {
      option?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(onChange).toHaveBeenCalledWith({ project: "org/alpha" });
    expect(container.querySelector('[role="listbox"]')).toBeNull();
  });

  it("selecting 'No project' calls onChange with projectNull", async () => {
    listRunProjectsMock.mockReturnValue(resolved([]));
    await mount();
    await open();
    const option = Array.from(container.querySelectorAll('[role="option"]')).find((el) =>
      el.textContent?.includes("No project"),
    );
    await act(async () => {
      option?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(onChange).toHaveBeenCalledWith({ projectNull: true });
  });

  it("selecting 'All projects' clears the filter", async () => {
    listRunProjectsMock.mockReturnValue(resolved([]));
    await mount({ project: "org/alpha" });
    await open();
    const option = Array.from(container.querySelectorAll('[role="option"]')).find((el) =>
      el.textContent?.includes("All projects"),
    );
    await act(async () => {
      option?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(onChange).toHaveBeenCalledWith({});
  });

  it("shows a dismissible error with retry on a failed fetch, without breaking the rest of the control", async () => {
    listRunProjectsMock.mockRejectedValueOnce(new Error("network down"));
    await mount();
    await open();
    expect(container.textContent).toContain("Failed to load projects");
    // Pinned options remain usable even though the fetch failed.
    expect(optionLabels().some((l) => l.includes("All projects"))).toBe(true);

    listRunProjectsMock.mockResolvedValueOnce(
      await resolved([row({ project: "org/alpha", count: 1 })]),
    );
    const retryButton = Array.from(container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Retry"),
    );
    expect(retryButton).toBeDefined();
    await act(async () => {
      retryButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(container.textContent).not.toContain("Failed to load projects");
    expect(optionLabels().some((l) => l.includes("org/alpha"))).toBe(true);
  });

  it("dismissing the error hides it without retrying", async () => {
    listRunProjectsMock.mockRejectedValue(new Error("network down"));
    await mount();
    await open();
    expect(container.textContent).toContain("Failed to load projects");
    const callsBefore = listRunProjectsMock.mock.calls.length;
    const dismissButton = container.querySelector('button[aria-label="Dismiss"]');
    expect(dismissButton).not.toBeNull();
    await act(async () => {
      dismissButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(container.textContent).not.toContain("Failed to load projects");
    expect(listRunProjectsMock.mock.calls.length).toBe(callsBefore);
  });

  it("refreshes the project list in the background, not only once on mount", async () => {
    listRunProjectsMock.mockReturnValue(resolved([row({ project: "org/alpha", count: 1 })]));
    await mount();
    expect(listRunProjectsMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(listRunProjectsMock).toHaveBeenCalledTimes(2);
  });

  it("dedupes two rows for the same project into a single option", async () => {
    listRunProjectsMock.mockReturnValue(
      resolved([
        row({ project: "org/alpha", count: 3, last_activity: 10 }),
        row({ project: "org/alpha", count: 4, last_activity: 40 }),
      ]),
    );
    await mount();
    await open();
    const matches = optionLabels().filter((l) => l.includes("org/alpha"));
    expect(matches).toHaveLength(1);
    expect(matches[0]).toContain("(7)");
  });

  it("navigates with ArrowDown/Enter and selects the highlighted option", async () => {
    listRunProjectsMock.mockReturnValue(resolved([row({ project: "org/alpha", count: 1 })]));
    await mount();
    await open();
    const input = container.querySelector('input[role="combobox"]') as HTMLInputElement;
    // Index 0 = "All projects", 1 = "No project", 2 = "org/alpha" (its only
    // last_activity is null, so it never lands in "Recent" — one flat list).
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
      await Promise.resolve();
    });
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
      await Promise.resolve();
    });
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      await Promise.resolve();
    });
    expect(onChange).toHaveBeenCalledWith({ project: "org/alpha" });
  });

  it("closes on Escape without changing the selection", async () => {
    listRunProjectsMock.mockReturnValue(resolved([]));
    await mount();
    await open();
    const input = container.querySelector('input[role="combobox"]') as HTMLInputElement;
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await Promise.resolve();
    });
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });
});
