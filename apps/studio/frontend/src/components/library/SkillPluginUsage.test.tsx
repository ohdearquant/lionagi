import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import enMessages from "@/messages/en.json";
import type { InvocationSummary } from "@/lib/api";

const api = vi.hoisted(() => ({
  getSkill: vi.fn(),
  getDefinition: vi.fn(),
  getPlugin: vi.fn(),
  getPluginAgent: vi.fn(),
  getDefinitionVersion: vi.fn(),
  getPluginSkill: vi.fn(),
  rollbackDefinition: vi.fn(),
  saveDefinition: vi.fn(),
  validateSkill: vi.fn(),
  listInvocations: vi.fn(),
}));
vi.mock("@/lib/api", () => api);
vi.mock("@tanstack/react-router", () => ({
  Link: ({ children }: { children: ReactNode }) => <a href="/fleet">{children}</a>,
}));
vi.mock("@/components/ui/Markdown", () => ({
  default: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

const { SkillDetail } = await import("./SkillDetail");
const { PluginDetail } = await import("./PluginDetail");

function invocation(index: number): InvocationSummary {
  return {
    id: `invocation-${index}`,
    skill: `recorded-skill-${index}`,
    plugin: "example",
    prompt: null,
    started_at: 1200 - index * 60,
    ended_at: 1799,
    status: "completed",
    session_count: 1,
    created_at: 1200 - index * 60,
    updated_at: 1800 - index,
    node_metadata: null,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const empty = {
  invocations: [],
  total: 0,
  completed_total: 0,
  finished_total: 0,
  latest_started_at: null,
};
const populated = {
  invocations: Array.from({ length: 5 }, (_, index) => invocation(index)),
  total: 320,
  completed_total: 240,
  finished_total: 300,
  latest_started_at: 1740,
};

describe.each(["skill", "plugin"] as const)("%s recorded usage", (kind) => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.spyOn(Date, "now").mockReturnValue(1800 * 1000);
    Object.values(api).forEach((fn) => fn.mockReset());
    api.getSkill.mockResolvedValue({
      name: "example",
      description: "Example skill",
      path: "skills/example/SKILL.md",
      content: "Instructions",
      allowed_tools: [],
    });
    api.getDefinition.mockResolvedValue({
      kind: "skill",
      name: "example",
      path: "skills/example/SKILL.md",
      content: "Instructions",
      version: null,
      versions: null,
      history_available: false,
    });
    api.getPlugin.mockResolvedValue({
      name: "example",
      description: "Example plugin",
      version: "1",
      source: "marketplace",
      skill_count: 0,
      agent_count: 0,
      has_hooks: false,
      has_mcp: false,
      path: "plugins/example",
      skills: [],
      agents: [],
      hooks: null,
      mcp: null,
      readme: null,
    });
    api.listInvocations.mockResolvedValue(empty);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function render(name = "example") {
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          {kind === "skill" ? <SkillDetail name={name} /> : <PluginDetail name={name} />}
        </IntlProvider>,
      );
    });
  }

  function metric(label: string) {
    const heading = [...container.querySelectorAll(".ui-uppercase")].find(
      (element) => element.textContent === label,
    );
    expect(heading, `missing metric ${label}`).toBeDefined();
    return heading!.parentElement!.parentElement!.lastElementChild!.textContent;
  }

  it("describes empty results as recorded activity with unknown tracking coverage", async () => {
    await render();
    expect(metric("Recorded invocations")).toBe("Not recorded");
    expect(metric("Last tracked use")).toBe("Not recorded");
    expect(container.textContent).toContain(
      "Tracking coverage for this skill or plugin is unknown",
    );
    expect(container.textContent).toContain("No recorded invocations.");
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(api.listInvocations).toHaveBeenCalledWith({
      [kind]: "example",
      limit: 5,
      includeUsage: true,
    });
  });

  it("keeps a failed read distinct from empty and retries only the usage request", async () => {
    api.listInvocations.mockRejectedValueOnce(new Error("Private transport detail"));
    await render();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "Recorded usage is unavailable.",
    );
    expect(container.textContent).not.toContain("Private transport detail");
    expect(container.textContent).not.toContain("No recorded invocations.");
    expect(container.textContent).not.toContain("No records");
    expect(container.textContent).not.toContain("Recorded success rate");
    const retry = [...container.querySelectorAll("button")].find(
      (button) => button.textContent === "Retry",
    );
    expect(retry).toBeDefined();
    await act(async () => retry!.click());
    expect(metric("Recorded invocations")).toBe("Not recorded");
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(api.listInvocations).toHaveBeenCalledTimes(2);
    expect(kind === "skill" ? api.getSkill : api.getPlugin).toHaveBeenCalledTimes(1);
  });

  it("uses all matching records for totals and last tracked use, with five recent rows", async () => {
    api.listInvocations.mockResolvedValue(populated);
    await render();
    expect(metric("Recorded invocations")).toBe("320");
    expect(metric("Recorded success rate")).toBe("80%");
    expect(metric("Last tracked use")).toBe("1m");
    const rows = [...container.querySelectorAll('a[href="/fleet"]')];
    expect(rows).toHaveLength(5);
    expect(rows[0].lastElementChild?.textContent).toBe("0s");
    expect(container.textContent).toContain("Recently updated records");
    expect(container.textContent).toContain("Recent ages show updates");
    expect(api.listInvocations).toHaveBeenCalledWith({
      [kind]: "example",
      limit: 5,
      includeUsage: true,
    });
  });

  it.each([
    { state: "omitted", aggregate: {} },
    { state: "null", aggregate: { latest_started_at: null } },
  ])("does not infer last tracked use when the aggregate is $state", async ({ aggregate }) => {
    api.listInvocations.mockResolvedValue({
      invocations: populated.invocations,
      total: populated.total,
      completed_total: populated.completed_total,
      ...aggregate,
    });
    await render();
    expect(metric("Last tracked use")).toBe("Unknown");
    expect(metric("Recorded invocations")).toBe("320");
  });

  it("counts only completed records as successes among finished records", async () => {
    api.listInvocations.mockResolvedValue({
      ...populated,
      total: 10,
      completed_total: 2,
      finished_total: 7,
    });
    await render();
    expect(metric("Recorded success rate")).toBe("29%");
  });

  it.each([
    { state: "absent", fields: {} },
    { state: "null", fields: { finished_total: null } },
    { state: "zero", fields: { finished_total: 0 } },
  ])("does not infer success rate when finished count is $state", async ({ fields }) => {
    const older = { ...populated } as Partial<typeof populated>;
    delete older.finished_total;
    api.listInvocations.mockResolvedValue({ ...older, ...fields });
    await render();
    expect(metric("Recorded success rate")).toBe("—");
  });

  it("does not display an old selection's late failure", async () => {
    const previous = deferred<typeof empty>();
    api.listInvocations.mockReturnValueOnce(previous.promise).mockResolvedValue(populated);
    await render();
    expect(metric("Recorded invocations")).toBe("—");
    await render("another");
    await act(async () => previous.reject(new Error("Late failure")));
    expect(metric("Recorded invocations")).toBe("320");
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(api.listInvocations).toHaveBeenLastCalledWith({
      [kind]: "another",
      limit: 5,
      includeUsage: true,
    });
  });
});
