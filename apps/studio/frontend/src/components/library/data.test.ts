import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listAgents: vi.fn(),
  listBuiltinPlaybooks: vi.fn(),
  listPlaybooks: vi.fn(),
  listWorkflowDefs: vi.fn(),
  listSkills: vi.fn(),
  listPlugins: vi.fn(),
  listEngineDefs: vi.fn(),
  listMcpServers: vi.fn(),
}));

vi.mock("@/lib/api", () => api);

import { loadLibraryCatalogs } from "./data";

beforeEach(() => {
  vi.resetAllMocks();
  api.listAgents.mockResolvedValue({ agents: [{ name: "reviewer", model: "gpt-5" }] });
  api.listBuiltinPlaybooks.mockResolvedValue({
    playbooks: [{ name: "builtin-review", description: "Bundled" }],
  });
  api.listPlaybooks.mockResolvedValue({
    playbooks: [{ name: "custom-review", description: "Local" }],
  });
  api.listWorkflowDefs.mockResolvedValue([]);
  api.listSkills.mockResolvedValue({ skills: [{ name: "tdd", description: "Test first" }] });
  api.listPlugins.mockResolvedValue({
    plugins: [{ name: "orchestrate", description: "Parallel work", version: "1.0.0" }],
  });
  api.listEngineDefs.mockResolvedValue([]);
  api.listMcpServers.mockResolvedValue({
    servers: [{ name: "local", transport: "stdio", enabled: true, command: "li" }],
  });
});

describe("loadLibraryCatalogs", () => {
  it("loads only the agent catalog for the agent tab", async () => {
    const result = await loadLibraryCatalogs("agent");

    expect(api.listAgents).toHaveBeenCalledOnce();
    expect(api.listBuiltinPlaybooks).not.toHaveBeenCalled();
    expect(api.listPlaybooks).not.toHaveBeenCalled();
    expect(api.listWorkflowDefs).not.toHaveBeenCalled();
    expect(api.listSkills).not.toHaveBeenCalled();
    expect(api.listPlugins).not.toHaveBeenCalled();
    expect(api.listEngineDefs).not.toHaveBeenCalled();
    expect(api.listMcpServers).not.toHaveBeenCalled();
    expect(result.items.map((item) => item.key)).toEqual(["agent:reviewer"]);
  });

  it("loads both playbook sources and no unrelated catalogs for the playbook tab", async () => {
    const result = await loadLibraryCatalogs("playbook");

    expect(api.listBuiltinPlaybooks).toHaveBeenCalledOnce();
    expect(api.listPlaybooks).toHaveBeenCalledOnce();
    expect(api.listAgents).not.toHaveBeenCalled();
    expect(api.listWorkflowDefs).not.toHaveBeenCalled();
    expect(api.listSkills).not.toHaveBeenCalled();
    expect(api.listPlugins).not.toHaveBeenCalled();
    expect(api.listEngineDefs).not.toHaveBeenCalled();
    expect(api.listMcpServers).not.toHaveBeenCalled();
    expect(result.items.map((item) => item.key)).toEqual([
      "playbook:builtin:builtin-review",
      "playbook:custom:custom-review",
    ]);
  });

  it.each([
    ["skill", "listSkills", "skill:tdd"],
    ["plugin", "listPlugins", "plugin:orchestrate"],
    ["mcp", "listMcpServers", "mcp:local"],
  ] as const)("loads only the %s catalog", async (tab, method, expectedKey) => {
    const result = await loadLibraryCatalogs(tab);

    const called = Object.entries(api)
      .filter(([, mock]) => mock.mock.calls.length > 0)
      .map(([name]) => name);
    expect(called).toEqual([method]);
    expect(result.items.map((item) => item.key)).toEqual([expectedKey]);
  });

  it("loads every supported visible catalog for All without unfinished catalogs", async () => {
    const result = await loadLibraryCatalogs("all");

    expect(api.listAgents).toHaveBeenCalledOnce();
    expect(api.listBuiltinPlaybooks).toHaveBeenCalledOnce();
    expect(api.listPlaybooks).toHaveBeenCalledOnce();
    expect(api.listSkills).toHaveBeenCalledOnce();
    expect(api.listPlugins).toHaveBeenCalledOnce();
    expect(api.listMcpServers).toHaveBeenCalledOnce();
    expect(api.listWorkflowDefs).not.toHaveBeenCalled();
    expect(api.listEngineDefs).not.toHaveBeenCalled();
    expect(result.items).toHaveLength(6);
  });

  it("keeps fulfilled active catalogs while marking an active-tab failure degraded", async () => {
    api.listPlaybooks.mockRejectedValue(new Error("custom catalog unavailable"));

    const result = await loadLibraryCatalogs("playbook");

    expect(result.degraded).toBe(true);
    expect(result.items.map((item) => item.key)).toEqual(["playbook:builtin:builtin-review"]);
  });

  it.each(["workflow", "engine"] as const)(
    "does not request the unfinished %s catalog",
    async (tab) => {
      const result = await loadLibraryCatalogs(tab);

      expect(Object.values(api).every((mock) => mock.mock.calls.length === 0)).toBe(true);
      expect(result).toMatchObject({ items: [], degraded: false });
    },
  );
});
