import { act, StrictMode, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const router = vi.hoisted(() => ({
  search: {} as Record<string, string | undefined>,
  navigate: vi.fn(),
  mounted: true,
  strict: false,
}));

const mutations = vi.hoisted(() => ({
  createAgent: vi.fn(),
  createWorkflow: vi.fn(),
  createMcp: vi.fn(),
  deleteAgent: vi.fn(),
  deleteMcp: vi.fn(),
  clonePlaybook: vi.fn(),
}));

const api = vi.hoisted(() => ({
  listAgents: vi.fn(),
  listWorkflowDefs: vi.fn(),
  listSkills: vi.fn(),
  listPlugins: vi.fn(),
  listEngineDefs: vi.fn(),
  listBuiltinPlaybooks: vi.fn(),
  listPlaybooks: vi.fn(),
  listMcpServers: vi.fn(),
}));

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    createFileRoute: () => (options: Record<string, unknown>) => ({
      ...options,
      useSearch: () => router.search,
    }),
    useNavigate: () => router.navigate,
    Link: ({ children, search }: { children: ReactNode; search: typeof router.search }) => (
      <button type="button" onClick={() => router.navigate({ search: () => search })}>
        {children}
      </button>
    ),
  };
});
vi.mock("use-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("@/lib/api", () => ({ ...api, ...mutations }));
vi.mock("@/components/ui/SplitPane", () => ({
  default: ({
    master,
    detail,
    detailActive,
  }: {
    master: ReactNode;
    detail: ReactNode;
    detailActive?: boolean;
  }) => (
    <>
      <div data-testid="master-pane">{master}</div>
      <div data-testid="detail-pane">{detail}</div>
      <div data-testid="split-pane" data-detail-active={String(detailActive ?? false)} />
    </>
  ),
}));
vi.mock("@/components/library/AgentDetail", () => ({
  AgentDetail: ({
    agent,
    onBack,
    onDeleted,
  }: {
    agent: { name: string; model?: string };
    onBack?: () => void;
    onDeleted: () => void;
  }) => (
    <div>
      <div data-testid="agent-detail" data-model={agent.model}>
        {agent.name}
      </div>
      {onBack && (
        <button type="button" onClick={onBack}>
          drawer.back
        </button>
      )}
      <button
        type="button"
        data-testid="delete-agent"
        onClick={() => void mutations.deleteAgent().then(onDeleted)}
      >
        delete agent
      </button>
    </div>
  ),
}));
vi.mock("@/components/AgentProfileForm", () => ({
  default: ({ onSave }: { onSave: (data: { name: string }) => Promise<void> }) => (
    <button
      type="button"
      data-testid="submit-agent"
      onClick={() => void onSave({ name: "created-agent" })}
    >
      save agent
    </button>
  ),
}));
vi.mock("@/components/library/WorkflowDetail", () => ({
  WorkflowDetail: ({ id, onDeleted }: { id: string; onDeleted?: (name: string) => void }) => (
    <>
      <div data-testid="workflow-detail">{id}</div>
      <button type="button" data-testid="delete-workflow" onClick={() => onDeleted?.("saved-flow")}>
        delete workflow
      </button>
    </>
  ),
  CreateWorkflowPanel: ({
    onCreated,
  }: {
    onCreated: (workflow: { id: string; name: string; created_at: number }) => void;
  }) => (
    <button
      type="button"
      data-testid="submit-workflow"
      onClick={() =>
        void mutations
          .createWorkflow()
          .then(() => onCreated({ id: "created-flow-id", name: "created-flow", created_at: 1 }))
      }
    >
      create workflow
    </button>
  ),
}));
vi.mock("@/components/library/PlaybookTemplateDetail", () => ({
  PlaybookTemplateDetail: ({
    name,
    isBuiltin,
    onCloned,
  }: {
    name: string;
    isBuiltin: boolean;
    onCloned: (name: string) => void;
  }) => (
    <div data-testid="playbook-detail" data-builtin={String(isBuiltin)}>
      {name}
      <button
        type="button"
        data-testid="clone-playbook"
        onClick={() => void mutations.clonePlaybook().then(() => onCloned("cloned-playbook"))}
      >
        clone playbook
      </button>
    </div>
  ),
}));
vi.mock("@/components/library/McpServerDetail", () => ({
  McpServerDetail: ({ name, onDeleted }: { name: string; onDeleted: () => void }) => (
    <div data-testid="mcp-detail">
      {name}
      <button
        type="button"
        data-testid="delete-mcp"
        onClick={() => void mutations.deleteMcp().then(onDeleted)}
      >
        delete mcp
      </button>
    </div>
  ),
  CreateMcpServerPanel: ({ onCreated }: { onCreated: (name: string) => void }) => (
    <button
      type="button"
      data-testid="submit-mcp"
      onClick={() => void mutations.createMcp().then(() => onCreated("created-mcp"))}
    >
      create mcp
    </button>
  ),
}));
vi.mock("@/components/library/SkillDetail", () => ({ SkillDetail: () => null }));
vi.mock("@/components/library/PluginDetail", () => ({ PluginDetail: () => null }));
vi.mock("@/components/library/HooksView", () => ({
  default: () => <div>hooks configuration</div>,
}));

// TanStack Router generates the split module when loading the route.
// @ts-expect-error virtual module generated by @tanstack/router-plugin
const { component: LibraryPage } = await import("./library.tsx?tsr-split=component");

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("Library catalog loading", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    Object.values(api).forEach((mock) => mock.mockReset());
    Object.values(mutations).forEach((mock) => mock.mockReset().mockResolvedValue(undefined));
    router.search = {};
    router.navigate.mockReset();
    router.mounted = true;
    router.strict = false;
    router.navigate.mockImplementation(
      ({ search }: { search: (previous: typeof router.search) => typeof router.search }) => {
        router.search = search(router.search);
        if (router.mounted) {
          root.render(
            router.strict ? (
              <StrictMode>
                <LibraryPage />
              </StrictMode>
            ) : (
              <LibraryPage />
            ),
          );
        }
        return Promise.resolve();
      },
    );
    api.listAgents.mockResolvedValue({ agents: [] });
    api.listBuiltinPlaybooks.mockResolvedValue({ playbooks: [] });
    api.listPlaybooks.mockResolvedValue({ playbooks: [] });
    api.listWorkflowDefs.mockResolvedValue([]);
    api.listSkills.mockResolvedValue({ skills: [] });
    api.listPlugins.mockResolvedValue({ plugins: [] });
    api.listEngineDefs.mockResolvedValue([]);
    api.listMcpServers.mockResolvedValue({ servers: [] });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    if (router.mounted) act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function renderLibrary(search: Record<string, string>) {
    router.search = { ...search };
    await act(async () =>
      root.render(
        router.strict ? (
          <StrictMode>
            <LibraryPage />
          </StrictMode>
        ) : (
          <LibraryPage />
        ),
      ),
    );
    await flush();
  }

  function expectRequests(wanted: (keyof typeof api)[], count = 1) {
    for (const [name, mock] of Object.entries(api)) {
      expect(mock, name).toHaveBeenCalledTimes(
        wanted.includes(name as keyof typeof api) ? count : 0,
      );
    }
  }

  function retry() {
    const button = container.querySelector<HTMLButtonElement>('[role="alert"] button');
    expect(button).not.toBeNull();
    return button!;
  }

  it.each([
    ["agent", ["listAgents"]],
    ["playbook", ["listBuiltinPlaybooks", "listPlaybooks"]],
    ["skill", ["listSkills"]],
    ["plugin", ["listPlugins"]],
    ["mcp", ["listMcpServers"]],
    ["hooks", []],
    ["workflow", []],
    ["engine", []],
    [
      "all",
      [
        "listAgents",
        "listBuiltinPlaybooks",
        "listPlaybooks",
        "listSkills",
        "listPlugins",
        "listMcpServers",
      ],
    ],
  ] as [string, (keyof typeof api)[]][])(
    "requests only the sources for %s",
    async (tab, wanted) => {
      await renderLibrary({ tab });
      expectRequests(wanted);
    },
  );

  it("does not wait for or report failures from inactive catalogs", async () => {
    api.listAgents.mockResolvedValue({ agents: [{ name: "active-agent" }] });
    api.listPlugins.mockRejectedValue(new Error("plugin catalog unavailable"));
    api.listWorkflowDefs.mockReturnValue(new Promise(() => {}));
    api.listEngineDefs.mockReturnValue(new Promise(() => {}));
    await renderLibrary({ tab: "agent" });
    expectRequests(["listAgents"]);
    expect(container.querySelector("tbody")?.textContent).toContain("active-agent");
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("preserves a selected deep link while the next tab is loading", async () => {
    api.listSkills.mockResolvedValue({ skills: [{ name: "old-skill" }] });
    await renderLibrary({ tab: "skill" });
    const agents = deferred<{ agents: { name: string }[] }>();
    api.listAgents.mockReturnValue(agents.promise);
    const selection = "agent:Réview: alpha/beta?";
    await renderLibrary({ tab: "agent", sel: selection });
    expect(router.search).toEqual({ tab: "agent", sel: selection });
    expect(container.querySelector("tbody")).toBeNull();
    agents.resolve({ agents: [{ name: "first-agent" }, { name: "Réview: alpha/beta?" }] });
    await flush();
    expect(router.search).toEqual({ tab: "agent", sel: selection });
    expect(container.querySelector('[data-testid="agent-detail"]')?.textContent).toBe(
      "Réview: alpha/beta?",
    );
    expect(api.listAgents).toHaveBeenCalledOnce();
    expect(api.listSkills).toHaveBeenCalledOnce();
  });

  it("auto-selects the first row without opening the detail pane", async () => {
    api.listAgents.mockResolvedValue({
      agents: [{ name: "first-agent" }, { name: "second-agent" }],
    });
    await renderLibrary({ tab: "agent" });
    // The row fills the detail pane without being written to the URL, so a
    // reload of an unselected tab cannot turn the fallback into a request.
    expect(router.search).toEqual({ tab: "agent" });
    expect(container.querySelector('[data-testid="agent-detail"]')?.textContent).toBe(
      "first-agent",
    );
    expect(
      container.querySelector('[data-testid="split-pane"]')?.getAttribute("data-detail-active"),
    ).toBe("false");

    await act(async () => container.querySelectorAll<HTMLTableRowElement>("tbody tr")[1].click());
    expect(router.search.sel).toBe("agent:second-agent");
    expect(
      container.querySelector('[data-testid="split-pane"]')?.getAttribute("data-detail-active"),
    ).toBe("true");
  });

  it("opens the detail pane for a selection named by the URL", async () => {
    api.listAgents.mockResolvedValue({
      agents: [{ name: "first-agent" }, { name: "second-agent" }],
    });
    await renderLibrary({ tab: "agent", sel: "agent:first-agent" });
    expect(
      container.querySelector('[data-testid="split-pane"]')?.getAttribute("data-detail-active"),
    ).toBe("true");
  });

  it("opens an explicit link even when another tab auto-selected the same item", async () => {
    api.listAgents.mockResolvedValue({ agents: [{ name: "saved-agent" }] });
    api.listSkills.mockResolvedValue({ skills: [{ name: "saved-skill" }] });
    await renderLibrary({ tab: "all" });
    expect(container.querySelector('[data-testid="agent-detail"]')?.textContent).toBe(
      "saved-agent",
    );
    expect(detailActiveAttr()).toBe("false");
    await act(async () =>
      router.navigate({ search: () => ({ tab: "agent", sel: "agent:saved-agent" }) }),
    );
    await flush();
    expect(detailActiveAttr()).toBe("true");
  });

  it.each(["bogus", "agent:missing-agent"])(
    "treats a selection that names no row as no selection (%s)",
    async (sel) => {
      api.listAgents.mockResolvedValue({
        agents: [{ name: "first-agent" }, { name: "second-agent" }],
      });
      await renderLibrary({ tab: "agent", sel });
      // The catalog stays up with its first row shown, as it would without a
      // selection, and no pane hides it behind a detail with nothing to show.
      expect(detailActiveAttr()).toBe("false");
      expect(container.querySelector("tbody")?.textContent).toContain("first-agent");
      expect(container.querySelector('[data-testid="agent-detail"]')?.textContent).toBe(
        "first-agent",
      );
      expect(container.querySelectorAll("tbody tr")[0].getAttribute("aria-selected")).toBe("true");
    },
  );

  it("keeps a hidden workflow detail on its own tab when the catalog has it", async () => {
    api.listWorkflowDefs.mockResolvedValue([{ id: "workflow-id", name: "saved-flow" }]);
    await renderLibrary({ tab: "workflow", sel: "workflow:saved-flow" });
    expect(container.querySelector('[data-testid="workflow-detail"]')?.textContent).toBe(
      "workflow-id",
    );
    expect(detailActiveAttr()).toBe("true");
  });

  it("does not open a hidden workflow its catalog does not have", async () => {
    api.listAgents.mockResolvedValue({ agents: [{ name: "first-agent" }] });
    await renderLibrary({ tab: "all", sel: "workflow:missing-flow" });
    expect(api.listWorkflowDefs).toHaveBeenCalledOnce();
    expect(detailActiveAttr()).toBe("false");
    expect(container.querySelector('[data-testid="workflow-detail"]')).toBeNull();
    expect(container.querySelector("tbody")?.textContent).toContain("first-agent");
  });

  it("does not render another kind's selection on the active tab", async () => {
    api.listSkills.mockResolvedValue({ skills: [{ name: "first-skill" }] });
    await renderLibrary({ tab: "skill", sel: "agent:first-agent" });
    expect(api.listAgents).not.toHaveBeenCalled();
    expect(container.querySelector('[data-testid="agent-detail"]')).toBeNull();
    expect(detailActiveAttr()).toBe("false");
  });

  it("marks the builtin playbook row a custom link falls back to", async () => {
    api.listBuiltinPlaybooks.mockResolvedValue({ playbooks: [{ name: "bundled" }] });
    await renderLibrary({ tab: "playbook", sel: "playbook:custom:bundled" });
    expect(detailActiveAttr()).toBe("false");
    expect(container.querySelectorAll("tbody tr")[0].getAttribute("aria-selected")).toBe("true");
    expect(
      container.querySelector('[data-testid="playbook-detail"]')?.getAttribute("data-builtin"),
    ).toBe("true");
  });

  it("keeps the fallback row off the URL in StrictMode", async () => {
    router.strict = true;
    api.listAgents.mockResolvedValue({ agents: [{ name: "first-agent" }] });
    await renderLibrary({ tab: "agent" });
    expect(router.search).toEqual({ tab: "agent" });
    expect(detailActiveAttr()).toBe("false");
    expect(container.querySelector('[data-testid="agent-detail"]')?.textContent).toBe(
      "first-agent",
    );
  });

  function detailActiveAttr() {
    return container
      .querySelector('[data-testid="split-pane"]')
      ?.getAttribute("data-detail-active");
  }

  it("keeps the catalog when the page mounts again at an address without a selection", async () => {
    api.listAgents.mockResolvedValue({
      agents: [{ name: "first-agent" }, { name: "second-agent" }],
    });
    await renderLibrary({ tab: "agent" });
    expect(detailActiveAttr()).toBe("false");

    // Opening a row and backing out of it returns the address to the tab.
    await act(async () => container.querySelector<HTMLTableRowElement>("tbody tr")!.click());
    expect(router.search.sel).toBe("agent:first-agent");
    const back = [...container.querySelectorAll("button")].find(
      (button) => button.textContent === "drawer.back",
    );
    await act(async () => back!.click());
    expect(router.search).toEqual({ tab: "agent" });

    // Mounting the page again at the same address must not turn the row on
    // screen into a request for detail, because the address never named it.
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<LibraryPage />));
    await flush();
    expect(detailActiveAttr()).toBe("false");
    expect(router.search).toEqual({ tab: "agent" });
  });

  it("opens the pane when the URL changes to another row on the same tab", async () => {
    api.listAgents.mockResolvedValue({
      agents: [{ name: "first-agent" }, { name: "second-agent" }],
    });
    await renderLibrary({ tab: "agent" });
    expect(detailActiveAttr()).toBe("false");

    await act(async () =>
      router.navigate({ search: () => ({ tab: "agent", sel: "agent:second-agent" }) }),
    );
    expect(detailActiveAttr()).toBe("true");

    await act(async () =>
      router.navigate({ search: () => ({ tab: "agent", sel: "agent:first-agent" }) }),
    );
    expect(detailActiveAttr()).toBe("true");
  });

  it("opens the pane for a click on the row the catalog auto-selected", async () => {
    api.listAgents.mockResolvedValue({ agents: [{ name: "first-agent" }] });
    await renderLibrary({ tab: "agent" });
    expect(detailActiveAttr()).toBe("false");

    await act(async () => container.querySelector<HTMLTableRowElement>("tbody tr")!.click());
    expect(router.search.sel).toBe("agent:first-agent");
    expect(detailActiveAttr()).toBe("true");
  });

  it("returns to the catalog when the drawer's back button is pressed", async () => {
    api.listAgents.mockResolvedValue({
      agents: [{ name: "first-agent" }, { name: "second-agent" }],
    });
    await renderLibrary({ tab: "agent" });
    await act(async () => container.querySelectorAll<HTMLTableRowElement>("tbody tr")[1].click());
    expect(detailActiveAttr()).toBe("true");

    const back = [...container.querySelectorAll("button")].find(
      (button) => button.textContent === "drawer.back",
    );
    expect(back).toBeDefined();
    await act(async () => back!.click());
    expect(router.search.sel).toBeUndefined();
    expect(detailActiveAttr()).toBe("false");
  });

  it("returns to the catalog when the All tab is picked while a row is open", async () => {
    api.listAgents.mockResolvedValue({ agents: [{ name: "first-agent" }] });
    await renderLibrary({});
    await act(async () => container.querySelector<HTMLTableRowElement>("tbody tr")!.click());
    expect(router.search).toEqual({ sel: "agent:first-agent" });
    expect(detailActiveAttr()).toBe("true");

    await clickTab("filterAll");
    expect(detailActiveAttr()).toBe("false");
  });

  it("returns to the catalog when the All tab is picked while its create form is open", async () => {
    await renderLibrary({ tab: "all" });
    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newWorkflow"),
    );
    expect(create).toBeDefined();
    await act(async () => create!.click());
    expect(detailActiveAttr()).toBe("true");

    await clickTab("filterAll");
    expect(container.querySelector('[data-testid="submit-workflow"]')).toBeNull();
    expect(detailActiveAttr()).toBe("false");
    expect(router.search).toEqual({});
  });

  it("does not flash the detail pane while a filtered catalog changes the auto-selection", async () => {
    api.listAgents.mockResolvedValue({
      agents: [{ name: "first-agent" }, { name: "second-agent" }],
    });
    await renderLibrary({ tab: "agent" });
    expect(detailActiveAttr()).toBe("false");

    const select = container.querySelector<HTMLInputElement>('input[type="text"]')!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(select, "second");
      select.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(container.querySelectorAll("tbody tr")).toHaveLength(1);
    // The fallback follows the visible row without asking for detail.
    expect(container.querySelector('[data-testid="agent-detail"]')?.textContent).toBe(
      "second-agent",
    );
    expect(router.search).toEqual({ tab: "agent" });
    expect(detailActiveAttr()).toBe("false");
  });

  it("keeps the create form open when it opens from an empty catalog", async () => {
    await renderLibrary({ tab: "workflow" });
    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newWorkflow"),
    );
    expect(create).toBeDefined();
    await act(async () => create!.click());
    expect(detailActiveAttr()).toBe("true");
    expect(container.querySelector('[data-testid="submit-workflow"]')).not.toBeNull();
  });

  it("drops a missing selection when the empty catalog opens its create form", async () => {
    await renderLibrary({ tab: "agent", sel: "agent:missing-agent" });
    // The toolbar's button and the empty catalog's own; the second is the one
    // only an empty catalog offers.
    const creates = [...container.querySelectorAll("button")].filter((button) =>
      button.textContent?.includes("newAgent"),
    );
    expect(creates).toHaveLength(2);
    const create = creates[1];
    await act(async () => create.click());
    expect(router.search).toEqual({ tab: "agent" });
    expect(detailActiveAttr()).toBe("true");

    const cancel = [...container.querySelectorAll("button")].find(
      (button) => button.textContent === "Cancel",
    );
    expect(cancel).toBeDefined();
    await act(async () => cancel!.click());
    expect(router.search).toEqual({ tab: "agent" });
    expect(detailActiveAttr()).toBe("false");
  });

  it("returns to the selected row when Back closes the create form", async () => {
    api.listAgents.mockResolvedValue({
      agents: [{ name: "first-agent" }, { name: "second-agent" }],
    });
    await renderLibrary({ tab: "agent" });
    await act(async () => container.querySelector<HTMLTableRowElement>("tbody tr")!.click());
    expect(router.search).toEqual({ tab: "agent", sel: "agent:first-agent" });

    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newAgent"),
    );
    expect(create).toBeDefined();
    await act(async () => create!.click());
    expect(router.search).toEqual({ tab: "agent" });
    expect(container.querySelector('[data-testid="submit-agent"]')).not.toBeNull();

    // Back restores the row the form's address dropped: its detail takes the
    // pane and the form is gone. The address is re-rendered from the history
    // entry, without the row being selected again, so the pane opens on its own.
    await renderLibrary({ tab: "agent", sel: "agent:first-agent" });
    expect(container.querySelector('[data-testid="submit-agent"]')).toBeNull();
    expect(container.querySelector('[data-testid="agent-detail"]')?.textContent).toBe(
      "first-agent",
    );
    expect(container.querySelectorAll("tbody tr")[0].getAttribute("aria-selected")).toBe("true");
    expect(detailActiveAttr()).toBe("true");

    // Back again reaches the catalog.
    await renderLibrary({ tab: "agent" });
    expect(container.querySelector('[data-testid="submit-agent"]')).toBeNull();
    expect(detailActiveAttr()).toBe("false");
  });

  it("revisits a tab from its cached rows with no new request or skeleton", async () => {
    api.listAgents.mockResolvedValue({ agents: [{ name: "alpha", model: "alpha-model" }] });
    api.listSkills.mockResolvedValue({ skills: [{ name: "beta-skill" }] });
    await renderLibrary({ tab: "agent", sel: "agent:alpha" });
    expect(api.listAgents).toHaveBeenCalledOnce();
    await renderLibrary({ tab: "skill", sel: "skill:beta-skill" });
    expect(api.listSkills).toHaveBeenCalledOnce();

    // The revisit request is never allowed to settle: the cached rows alone
    // must keep the tab out of its loading state.
    api.listAgents.mockReturnValue(new Promise(() => {}));
    await renderLibrary({ tab: "agent", sel: "agent:alpha" });
    expect(api.listAgents).toHaveBeenCalledOnce();
    expect(container.querySelector(".skeleton")).toBeNull();
    expect(container.querySelector("tbody")?.textContent).toContain("alpha");
    expect(
      container.querySelector<HTMLElement>('[data-testid="agent-detail"]')?.dataset.model,
    ).toBe("alpha-model");
    expect(api.listSkills).toHaveBeenCalledOnce();
  });

  it("re-requests only the source that failed when Retry is pressed", async () => {
    api.listAgents.mockResolvedValue({ agents: [{ name: "alpha" }] });
    api.listBuiltinPlaybooks.mockResolvedValue({ playbooks: [{ name: "bundled" }] });
    api.listPlaybooks.mockResolvedValue({ playbooks: [{ name: "custom" }] });
    api.listSkills.mockResolvedValue({ skills: [{ name: "beta-skill" }] });
    api.listPlugins.mockResolvedValue({ plugins: [] });
    api.listMcpServers.mockRejectedValueOnce(new Error("mcp catalog unavailable"));
    await renderLibrary({});
    expect(api.listMcpServers).toHaveBeenCalledOnce();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("filterMcp");

    await act(async () => retry().click());
    await flush();

    expect(api.listMcpServers).toHaveBeenCalledTimes(2);
    for (const source of [
      "listAgents",
      "listBuiltinPlaybooks",
      "listPlaybooks",
      "listSkills",
      "listPlugins",
    ] as const) {
      expect(api[source], source).toHaveBeenCalledOnce();
    }
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.querySelector("tbody")?.textContent).toContain("alpha");
  });

  it("closes the create form when the tab changes", async () => {
    await renderLibrary({ tab: "all" });
    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newWorkflow"),
    );
    expect(create).toBeDefined();
    await act(async () => create!.click());
    expect(container.querySelector('[data-testid="submit-workflow"]')).not.toBeNull();

    // The skill tab has no create affordance of its own, so a workflow form
    // still on screen here is a form that outlived its tab.
    await renderLibrary({ tab: "skill", sel: "skill:alpha" });
    expect(container.querySelector('[data-testid="submit-workflow"]')).toBeNull();
  });

  it("ignores a previous tab's late rejection", async () => {
    const plugins = deferred<{ plugins: [] }>();
    api.listPlugins.mockReturnValue(plugins.promise);
    await renderLibrary({ tab: "plugin" });
    api.listAgents.mockResolvedValue({ agents: [{ name: "active-agent" }] });
    await renderLibrary({ tab: "agent", sel: "agent:active-agent" });
    plugins.reject(new Error("previous tab failed"));
    await flush();
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.querySelector("tbody")?.textContent).toContain("active-agent");
    expect(router.search).toEqual({ tab: "agent", sel: "agent:active-agent" });
  });

  it("attributes active errors and retries without discarding the deep link", async () => {
    api.listAgents.mockRejectedValueOnce(new Error("catalog unavailable"));
    await renderLibrary({ tab: "agent", sel: "agent:selected-agent" });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("filterAgent");
    expect(router.search.sel).toBe("agent:selected-agent");
    api.listAgents.mockResolvedValue({ agents: [{ name: "selected-agent" }] });
    await act(async () => retry().click());
    await flush();
    expectRequests(["listAgents"], 2);
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.querySelector("tbody")?.textContent).toContain("selected-agent");
    expect(router.search.sel).toBe("agent:selected-agent");
  });

  it("invalidates a pending retry when another tab is selected", async () => {
    api.listAgents.mockRejectedValueOnce(new Error("catalog unavailable"));
    await renderLibrary({ tab: "agent" });
    const pendingRetry = deferred<{ agents: [] }>();
    api.listAgents.mockReturnValue(pendingRetry.promise);
    await act(async () => retry().click());
    api.listSkills.mockResolvedValue({ skills: [{ name: "active-skill" }] });
    await renderLibrary({ tab: "skill", sel: "skill:active-skill" });
    pendingRetry.reject(new Error("retry failed after navigation"));
    await flush();
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.querySelector("tbody")?.textContent).toContain("active-skill");
    expect(router.search).toEqual({ tab: "skill", sel: "skill:active-skill" });
  });

  it.each(["listBuiltinPlaybooks", "listPlaybooks"] as const)(
    "keeps partial playbook rows and retries when %s fails",
    async (failedSource) => {
      api.listBuiltinPlaybooks.mockResolvedValue({ playbooks: [{ name: "bundled" }] });
      api.listPlaybooks.mockResolvedValue({ playbooks: [{ name: "custom" }] });
      api[failedSource].mockRejectedValueOnce(new Error("source unavailable"));
      await renderLibrary({ tab: "playbook" });
      expectRequests(["listBuiltinPlaybooks", "listPlaybooks"]);
      expect(container.querySelector('[role="alert"]')?.textContent).toContain("filterPlaybook");
      expect(container.querySelectorAll("tbody tr")).toHaveLength(1);
      await act(async () => retry().click());
      await flush();
      // Only the source that failed is fetched again; the other keeps its rows.
      expect(api[failedSource]).toHaveBeenCalledTimes(2);
      const otherSource =
        failedSource === "listBuiltinPlaybooks" ? "listPlaybooks" : "listBuiltinPlaybooks";
      expect(api[otherSource]).toHaveBeenCalledOnce();
      expect(container.querySelector('[role="alert"]')).toBeNull();
      expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    },
  );

  it("preserves an explicit engine detail link without loading workflows", async () => {
    api.listEngineDefs.mockResolvedValue([
      { id: "engine-id", name: "saved-engine", kind: "local" },
    ]);
    await renderLibrary({ sel: "engine:saved-engine" });
    expect(api.listEngineDefs).toHaveBeenCalledOnce();
    expect(api.listWorkflowDefs).not.toHaveBeenCalled();
    expect(router.search.sel).toBe("engine:saved-engine");
    expect(container.textContent).toContain("saved-engine");
  });

  it("loads a hidden workflow only when an explicit detail link needs it", async () => {
    api.listWorkflowDefs.mockResolvedValue([{ id: "workflow-id", name: "saved-flow" }]);
    await renderLibrary({ sel: "workflow:saved-flow" });
    expect(api.listWorkflowDefs).toHaveBeenCalledOnce();
    expect(api.listEngineDefs).not.toHaveBeenCalled();
    expect(router.search.sel).toBe("workflow:saved-flow");
    expect(container.querySelector('[data-testid="workflow-detail"]')?.textContent).toBe(
      "workflow-id",
    );
  });

  const completionCases = [
    ["agent create", "agent", "createAgent", "submit-agent", "newAgent"],
    ["workflow create", "all", "createWorkflow", "submit-workflow", "newWorkflow"],
    ["MCP create", "mcp", "createMcp", "submit-mcp", "newMcpServer"],
    ["agent delete", "agent", "deleteAgent", "delete-agent", null],
    ["MCP delete", "mcp", "deleteMcp", "delete-mcp", null],
    ["playbook clone", "playbook", "clonePlaybook", "clone-playbook", null],
  ] as const;

  async function startCompletion(
    tab: string,
    action: keyof typeof mutations,
    buttonId: string,
    createLabel: string | null,
  ) {
    const completion = deferred<void>();
    mutations[action].mockReturnValue(completion.promise);
    api.listAgents.mockResolvedValue({ agents: [{ name: "existing-agent" }] });
    api.listMcpServers.mockResolvedValue({ servers: [{ name: "existing-mcp" }] });
    api.listBuiltinPlaybooks.mockResolvedValue({ playbooks: [{ name: "bundled" }] });
    api.listSkills.mockResolvedValue({
      skills: [{ name: "first-skill" }, { name: "active-skill" }],
    });
    await renderLibrary({ tab });
    if (createLabel) {
      const create = [...container.querySelectorAll("button")].find((button) =>
        button.textContent?.includes(createLabel),
      );
      expect(create).toBeDefined();
      await act(async () => create!.click());
    }
    const submit = container.querySelector<HTMLButtonElement>(`[data-testid="${buttonId}"]`);
    expect(submit).not.toBeNull();
    await act(async () => submit!.click());
    expect(mutations[action]).toHaveBeenCalledOnce();
    return completion;
  }

  it.each(completionCases)(
    "refreshes the current tab and keeps its second selection after a late %s",
    async (_name, tab, action, buttonId, createLabel) => {
      const completion = await startCompletion(tab, action, buttonId, createLabel);
      const skillTab = [...container.querySelectorAll("nav button")].find(
        (button) => button.textContent === "filterSkill",
      );
      await act(async () => (skillTab as HTMLButtonElement).click());
      await flush();
      await act(async () => container.querySelectorAll<HTMLTableRowElement>("tbody tr")[1].click());
      expect(router.search).toEqual({ tab: "skill", sel: "skill:active-skill" });
      const navigations = router.navigate.mock.calls.length;
      completion.resolve();
      await flush();
      expect(router.search).toEqual({ tab: "skill", sel: "skill:active-skill" });
      expect(container.querySelector("tbody")?.textContent).toContain("active-skill");
      expect(container.querySelector('[role="alert"]')).toBeNull();
      expect(router.navigate).toHaveBeenCalledTimes(navigations + 1);
      const initialSources =
        tab === "all"
          ? [
              "listAgents",
              "listBuiltinPlaybooks",
              "listPlaybooks",
              "listSkills",
              "listPlugins",
              "listMcpServers",
            ]
          : tab === "agent"
            ? ["listAgents"]
            : tab === "mcp"
              ? ["listMcpServers"]
              : ["listBuiltinPlaybooks", "listPlaybooks"];
      for (const [source, mock] of Object.entries(api)) {
        // The skill tab is visited once after the create; with its rows cached,
        // that visit adds no request, so only the completion's refresh reaches
        // it. Every other source keeps its single initial fetch.
        expect(mock, source).toHaveBeenCalledTimes(
          source === "listSkills" ? 2 : Number(initialSources.includes(source)),
        );
      }
    },
  );

  it.each(completionCases)(
    "does not navigate or refresh after unmount during %s",
    async (_name, tab, action, buttonId, createLabel) => {
      const completion = await startCompletion(tab, action, buttonId, createLabel);
      act(() => root.unmount());
      router.mounted = false;
      router.navigate.mockClear();
      const requests = Object.fromEntries(
        Object.entries(api).map(([name, mock]) => [name, mock.mock.calls.length]),
      );
      const selection = { ...router.search };
      completion.resolve();
      await flush();
      expect(router.navigate).not.toHaveBeenCalled();
      expect(router.search).toEqual(selection);
      for (const [source, mock] of Object.entries(api)) {
        expect(mock, source).toHaveBeenCalledTimes(requests[source]);
      }
    },
  );

  it.each(["workflow", "engine"] as const)(
    "keeps a failed hidden %s out of the master banner",
    async (kind) => {
      api.listAgents.mockResolvedValue({ agents: [{ name: "visible-agent" }] });
      api[kind === "workflow" ? "listWorkflowDefs" : "listEngineDefs"].mockRejectedValue(
        new Error("hidden catalog unavailable"),
      );
      await renderLibrary({ tab: "agent", sel: `${kind}:saved-item` });
      const master = container.querySelector('[data-testid="master-pane"]')!;
      const detail = container.querySelector('[data-testid="detail-pane"]')!;
      expect(master.querySelector('[role="alert"]')).toBeNull();
      expect(master.querySelector("tbody")?.textContent).toContain("visible-agent");
      expect(detail.querySelector('[role="alert"]')?.textContent).toContain("loadError");
      expect(detail.textContent).not.toContain("notFound");
      expect(router.search).toEqual({ tab: "agent", sel: `${kind}:saved-item` });
      expectRequests(["listAgents", kind === "workflow" ? "listWorkflowDefs" : "listEngineDefs"]);
      const source = kind === "workflow" ? "listWorkflowDefs" : "listEngineDefs";
      api[source].mockResolvedValue([{ id: "saved-id", name: "saved-item", kind: "local" }]);
      await act(async () =>
        detail.querySelector<HTMLButtonElement>("[role=alert] button")!.click(),
      );
      await flush();
      expect(api.listAgents).toHaveBeenCalledOnce();
      expect(api[source]).toHaveBeenCalledTimes(2);
      expect(detail.querySelector('[role="alert"]')).toBeNull();
      expect(master.querySelector("tbody")?.textContent).toContain("visible-agent");
    },
  );

  it("names only visible failures when visible and hidden catalogs fail together", async () => {
    api.listAgents.mockRejectedValue(new Error("agent catalog unavailable"));
    api.listEngineDefs.mockRejectedValue(new Error("engine catalog unavailable"));
    await renderLibrary({ tab: "agent", sel: "engine:saved-engine" });
    const banner = container.querySelector('[data-testid="master-pane"] [role="alert"]');
    expect(banner?.textContent).toContain("filterAgent");
    expect(banner?.textContent).not.toContain("filterEngine");
    expect(container.querySelector('[data-testid="detail-pane"] [role="alert"]')).not.toBeNull();
    api.listAgents.mockResolvedValue({ agents: [{ name: "visible-agent" }] });
    await act(async () => retry().click());
    await flush();
    expect(api.listAgents).toHaveBeenCalledTimes(2);
    expect(api.listEngineDefs).toHaveBeenCalledOnce();
    expect(container.querySelector('[data-testid="master-pane"] [role="alert"]')).toBeNull();
    expect(container.querySelector('[data-testid="detail-pane"] [role="alert"]')).not.toBeNull();
  });

  it("shows engine loading until the requested catalog settles", async () => {
    const engines = deferred<{ id: string; name: string; kind: string }[]>();
    api.listEngineDefs.mockReturnValue(engines.promise);
    api.listAgents.mockResolvedValue({ agents: [{ name: "visible-agent" }] });
    await renderLibrary({ tab: "agent", sel: "engine:saved-engine" });
    const detail = container.querySelector('[data-testid="detail-pane"]')!;
    expect(detail.querySelector('[role="status"]')?.textContent).toContain("loading");
    expect(detail.textContent).not.toContain("notFound");
    engines.resolve([{ id: "engine-id", name: "saved-engine", kind: "local" }]);
    await flush();
    expect(detail.querySelector('[role="status"]')).toBeNull();
    expect(detail.textContent).toContain("saved-engine");
    expect(router.search).toEqual({ tab: "agent", sel: "engine:saved-engine" });
  });

  it("shows engine not-found only after a successful empty catalog response", async () => {
    api.listAgents.mockResolvedValue({ agents: [{ name: "visible-agent" }] });
    await renderLibrary({ tab: "agent", sel: "engine:missing-engine" });
    const detail = container.querySelector('[data-testid="detail-pane"]')!;
    expect(detail.textContent).toContain("notFound");
    expect(detail.querySelector('[role="status"]')).toBeNull();
    expect(detail.querySelector('[role="alert"]')).toBeNull();
    expect(router.search).toEqual({ tab: "agent", sel: "engine:missing-engine" });
  });

  it("handles completion after StrictMode effect cleanup and setup", async () => {
    router.strict = true;
    const completion = await startCompletion(
      "all",
      "createWorkflow",
      "submit-workflow",
      "newWorkflow",
    );
    completion.resolve();
    await flush();
    expect(router.search.sel).toBe("workflow:created-flow");
    expect(container.querySelector('[data-testid="workflow-detail"]')?.textContent).toBe(
      "created-flow-id",
    );
  });

  it.each(["workflow", "engine"] as const)(
    "does not load a hidden %s detail on Hooks",
    async (kind) => {
      await renderLibrary({ tab: "hooks", sel: `${kind}:saved-item` });
      expectRequests([]);
      expect(container.textContent).toContain("hooks configuration");
      expect(container.querySelector('[data-testid="detail-pane"]')).toBeNull();
    },
  );

  it.each(["workflow", "engine"] as const)(
    "keeps an explicit %s detail on a visible agent tab without reloading visible rows",
    async (kind) => {
      api.listAgents.mockResolvedValue({
        agents: [{ name: "visible-agent", model: "visible-model" }],
      });
      api.listWorkflowDefs.mockResolvedValue([{ id: "workflow-id", name: "saved-flow" }]);
      api.listEngineDefs.mockResolvedValue([
        { id: "engine-id", name: "saved-engine", kind: "local", model: "engine-model" },
      ]);
      const name = kind === "workflow" ? "saved-flow" : "saved-engine";
      await renderLibrary({ tab: "agent", sel: `${kind}:${name}` });
      expect(router.search).toEqual({ tab: "agent", sel: `${kind}:${name}` });
      expect(api.listAgents).toHaveBeenCalledOnce();
      expectRequests(["listAgents", kind === "workflow" ? "listWorkflowDefs" : "listEngineDefs"]);
      if (kind === "workflow") {
        expect(container.querySelector('[data-testid="workflow-detail"]')?.textContent).toBe(
          "workflow-id",
        );
      } else {
        expect(container.textContent).toContain("engine-model");
      }
      await act(async () => container.querySelector<HTMLTableRowElement>("tbody tr")!.click());
      await flush();
      expect(router.search).toEqual({ tab: "agent", sel: "agent:visible-agent" });
      expect(api.listAgents).toHaveBeenCalledOnce();
      expect(container.querySelector("tbody")?.textContent).toContain("visible-agent");
      expect(
        container.querySelector<HTMLElement>('[data-testid="agent-detail"]')?.dataset.model,
      ).toBe("visible-model");
    },
  );

  it("leaves a hidden detail without reloading the All catalog", async () => {
    api.listAgents.mockResolvedValue({
      agents: [{ name: "visible-agent", model: "visible-model" }],
    });
    api.listWorkflowDefs.mockResolvedValue([{ id: "workflow-id", name: "saved-flow" }]);
    await renderLibrary({ sel: "workflow:saved-flow" });
    expect(container.querySelector('[data-testid="workflow-detail"]')?.textContent).toBe(
      "workflow-id",
    );
    await act(async () => container.querySelector<HTMLTableRowElement>("tbody tr")!.click());
    await flush();
    expectRequests([
      "listAgents",
      "listBuiltinPlaybooks",
      "listPlaybooks",
      "listSkills",
      "listPlugins",
      "listMcpServers",
      "listWorkflowDefs",
    ]);
    expect(router.search.sel).toBe("agent:visible-agent");
    expect(
      container.querySelector<HTMLElement>('[data-testid="agent-detail"]')?.dataset.model,
    ).toBe("visible-model");
  });

  it("ignores a previous tab's late success", async () => {
    const agents = deferred<{ agents: { name: string }[] }>();
    api.listAgents.mockReturnValue(agents.promise);
    await renderLibrary({ tab: "agent" });
    api.listSkills.mockResolvedValue({ skills: [{ name: "active-skill" }] });
    await renderLibrary({ tab: "skill", sel: "skill:active-skill" });
    agents.resolve({ agents: [{ name: "late-agent" }] });
    await flush();
    expect(container.querySelector("tbody")?.textContent).toContain("active-skill");
    expect(container.textContent).not.toContain("late-agent");
    expect(router.search).toEqual({ tab: "skill", sel: "skill:active-skill" });
  });

  it("clears agent model data when a same-tab reload fails", async () => {
    api.listAgents.mockResolvedValueOnce({ agents: [{ name: "saved-agent", model: "old-model" }] });
    await renderLibrary({ tab: "agent", sel: "agent:saved-agent" });
    expect(
      container.querySelector<HTMLElement>('[data-testid="agent-detail"]')?.dataset.model,
    ).toBe("old-model");
    api.listAgents.mockRejectedValue(new Error("agent catalog unavailable"));
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="delete-agent"]')!.click(),
    );
    await flush();
    await renderLibrary({ tab: "agent", sel: "agent:saved-agent" });
    expect(api.listAgents).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("filterAgent");
    expect(
      container.querySelector<HTMLElement>('[data-testid="agent-detail"]')?.dataset.model,
    ).toBe("");
  });

  it("keeps a selection made while a same-tab delete is pending", async () => {
    api.listAgents.mockResolvedValue({
      agents: [{ name: "first-agent" }, { name: "second-agent" }],
    });
    const deletion = deferred<void>();
    mutations.deleteAgent.mockReturnValue(deletion.promise);
    await renderLibrary({ tab: "agent", sel: "agent:first-agent" });
    expect(router.search.sel).toBe("agent:first-agent");

    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="delete-agent"]')!.click(),
    );
    expect(mutations.deleteAgent).toHaveBeenCalledOnce();

    await act(async () => container.querySelectorAll<HTMLTableRowElement>("tbody tr")[1].click());
    expect(router.search.sel).toBe("agent:second-agent");

    deletion.resolve();
    await flush();
    expect(router.search.sel).toBe("agent:second-agent");
    expect(container.querySelector<HTMLElement>('[data-testid="agent-detail"]')?.textContent).toBe(
      "second-agent",
    );
  });

  it("keeps a row picked while an MCP delete is in flight", async () => {
    api.listMcpServers.mockResolvedValue({
      servers: [{ name: "existing-mcp" }, { name: "second-mcp" }],
    });
    const deletion = deferred<void>();
    mutations.deleteMcp.mockReturnValue(deletion.promise);
    await renderLibrary({ tab: "mcp", sel: "mcp:existing-mcp" });

    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="delete-mcp"]')!.click(),
    );
    await act(async () => container.querySelectorAll<HTMLTableRowElement>("tbody tr")[1].click());
    expect(router.search.sel).toBe("mcp:second-mcp");

    deletion.resolve();
    await flush();
    expect(router.search).toEqual({ tab: "mcp", sel: "mcp:second-mcp" });
    expect(detailActiveAttr()).toBe("true");
    expect(container.querySelector('[data-testid="mcp-detail"]')?.firstChild?.textContent).toBe(
      "second-mcp",
    );
  });

  it("keeps a create form open when an MCP delete resolves behind it", async () => {
    api.listMcpServers.mockResolvedValue({ servers: [{ name: "existing-mcp" }] });
    const deletion = deferred<void>();
    mutations.deleteMcp.mockReturnValue(deletion.promise);
    await renderLibrary({ tab: "mcp", sel: "mcp:existing-mcp" });

    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="delete-mcp"]')!.click(),
    );
    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newMcpServer"),
    );
    await act(async () => create!.click());
    expect(router.search).toEqual({ tab: "mcp" });
    expect(container.querySelector('[data-testid="submit-mcp"]')).not.toBeNull();

    deletion.resolve();
    await flush();
    expect(container.querySelector('[data-testid="submit-mcp"]')).not.toBeNull();
    expect(router.search).toEqual({ tab: "mcp" });
    expect(detailActiveAttr()).toBe("true");
  });

  it("drops the selection when the delete resolves and it still names the deleted item", async () => {
    api.listAgents.mockResolvedValue({ agents: [{ name: "first-agent" }] });
    const deletion = deferred<void>();
    mutations.deleteAgent.mockReturnValue(deletion.promise);
    await renderLibrary({ tab: "agent", sel: "agent:first-agent" });

    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="delete-agent"]')!.click(),
    );
    deletion.resolve();
    await flush();
    expect(router.search.sel).toBeUndefined();
  });

  it("does not show a create form opened on one tab after a completion from another", async () => {
    const creation = deferred<void>();
    mutations.createWorkflow.mockReturnValue(creation.promise);
    api.listWorkflowDefs.mockResolvedValue([{ id: "workflow-id", name: "saved-flow" }]);
    api.listAgents.mockResolvedValue({ agents: [{ name: "first-agent" }] });
    await renderLibrary({});

    const workflowCreate = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newWorkflow"),
    );
    expect(workflowCreate).toBeDefined();
    await act(async () => workflowCreate!.click());

    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="submit-workflow"]')!.click(),
    );
    expect(mutations.createWorkflow).toHaveBeenCalledOnce();

    const agentTab = [...container.querySelectorAll("nav button")].find(
      (button) => button.textContent === "filterAgent",
    );
    await act(async () => (agentTab as HTMLButtonElement).click());
    await flush();

    const agentCreate = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newAgent"),
    );
    expect(agentCreate).toBeDefined();
    await act(async () => agentCreate!.click());
    expect(container.querySelector('[data-testid="submit-agent"]')).not.toBeNull();

    creation.resolve();
    await flush();

    expect(container.querySelector('[data-testid="submit-agent"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="submit-workflow"]')).toBeNull();
    expect(router.search.tab).toBe("agent");
  });

  it("clears engine model data when a late clone refresh fails", async () => {
    const completion = await startCompletion("playbook", "clonePlaybook", "clone-playbook", null);
    api.listEngineDefs.mockResolvedValueOnce([
      { id: "engine-id", name: "saved-engine", kind: "local", model: "old-engine-model" },
    ]);
    await renderLibrary({ sel: "engine:saved-engine" });
    expect(container.textContent).toContain("old-engine-model");
    api.listEngineDefs.mockRejectedValue(new Error("engine catalog unavailable"));
    completion.resolve();
    await flush();
    expect(api.listEngineDefs).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[data-testid="master-pane"] [role="alert"]')).toBeNull();
    expect(
      container.querySelector('[data-testid="detail-pane"] [role="alert"]')?.textContent,
    ).toContain("loadError");
    expect(container.textContent).not.toContain("old-engine-model");
    expect(router.search.sel).toBe("engine:saved-engine");
  });

  async function clickTab(label: string) {
    const button = [...container.querySelectorAll("nav button")].find(
      (element) => element.textContent === label,
    );
    expect(button).toBeDefined();
    await act(async () => (button as HTMLButtonElement).click());
    await flush();
  }

  it("refetches the tab where an agent create completed after the viewer left it", async () => {
    api.listAgents.mockResolvedValue({ agents: [{ name: "existing-agent" }] });
    const creation = deferred<void>();
    mutations.createAgent.mockReturnValue(creation.promise);
    await renderLibrary({ tab: "agent" });
    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newAgent"),
    );
    expect(create).toBeDefined();
    await act(async () => create!.click());
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="submit-agent"]')!.click(),
    );
    expect(mutations.createAgent).toHaveBeenCalledOnce();

    await clickTab("filterSkill");
    api.listAgents.mockResolvedValue({
      agents: [{ name: "existing-agent" }, { name: "created-agent" }],
    });
    creation.resolve();
    await flush();

    await clickTab("filterAgent");
    expect(api.listAgents).toHaveBeenCalledTimes(2);
    expect(container.querySelector("tbody")?.textContent).toContain("created-agent");
  });

  it("refetches the tab where an agent delete completed after the viewer left it", async () => {
    api.listAgents.mockResolvedValue({
      agents: [{ name: "existing-agent" }, { name: "doomed-agent" }],
    });
    const deletion = deferred<void>();
    mutations.deleteAgent.mockReturnValue(deletion.promise);
    await renderLibrary({ tab: "agent", sel: "agent:doomed-agent" });
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="delete-agent"]')!.click(),
    );
    expect(mutations.deleteAgent).toHaveBeenCalledOnce();

    await clickTab("filterSkill");
    api.listAgents.mockResolvedValue({ agents: [{ name: "existing-agent" }] });
    deletion.resolve();
    await flush();

    await clickTab("filterAgent");
    expect(api.listAgents).toHaveBeenCalledTimes(2);
    expect(container.querySelector("tbody")?.textContent).not.toContain("doomed-agent");
  });

  it("refetches the tab where an MCP create completed after the viewer left it", async () => {
    api.listMcpServers.mockResolvedValue({ servers: [{ name: "existing-mcp" }] });
    const creation = deferred<void>();
    mutations.createMcp.mockReturnValue(creation.promise);
    await renderLibrary({ tab: "mcp" });
    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newMcpServer"),
    );
    expect(create).toBeDefined();
    await act(async () => create!.click());
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="submit-mcp"]')!.click(),
    );
    expect(mutations.createMcp).toHaveBeenCalledOnce();

    await clickTab("filterSkill");
    api.listMcpServers.mockResolvedValue({
      servers: [{ name: "existing-mcp" }, { name: "created-mcp" }],
    });
    creation.resolve();
    await flush();

    await clickTab("filterMcp");
    expect(api.listMcpServers).toHaveBeenCalledTimes(2);
    expect(container.querySelector("tbody")?.textContent).toContain("created-mcp");
  });

  it("refetches the tab where an MCP delete completed after the viewer left it", async () => {
    api.listMcpServers.mockResolvedValue({
      servers: [{ name: "existing-mcp" }, { name: "doomed-mcp" }],
    });
    const deletion = deferred<void>();
    mutations.deleteMcp.mockReturnValue(deletion.promise);
    await renderLibrary({ tab: "mcp", sel: "mcp:doomed-mcp" });
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="delete-mcp"]')!.click(),
    );
    expect(mutations.deleteMcp).toHaveBeenCalledOnce();

    await clickTab("filterSkill");
    api.listMcpServers.mockResolvedValue({ servers: [{ name: "existing-mcp" }] });
    deletion.resolve();
    await flush();

    await clickTab("filterMcp");
    expect(api.listMcpServers).toHaveBeenCalledTimes(2);
    expect(container.querySelector("tbody")?.textContent).not.toContain("doomed-mcp");
  });

  it("refetches the tab where a playbook clone completed after the viewer left it", async () => {
    api.listBuiltinPlaybooks.mockResolvedValue({ playbooks: [{ name: "bundled" }] });
    api.listPlaybooks.mockResolvedValue({ playbooks: [] });
    const clone = deferred<void>();
    mutations.clonePlaybook.mockReturnValue(clone.promise);
    await renderLibrary({ tab: "playbook", sel: "playbook:builtin:bundled" });
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="clone-playbook"]')!.click(),
    );
    expect(mutations.clonePlaybook).toHaveBeenCalledOnce();

    await clickTab("filterSkill");
    api.listPlaybooks.mockResolvedValue({ playbooks: [{ name: "cloned-playbook" }] });
    clone.resolve();
    await flush();

    await clickTab("filterPlaybook");
    expect(api.listPlaybooks).toHaveBeenCalledTimes(2);
    expect(container.querySelector("tbody")?.textContent).toContain("cloned-playbook");
  });

  it("keeps a row picked while a same-tab agent create is pending", async () => {
    api.listAgents.mockResolvedValue({
      agents: [{ name: "first-agent" }, { name: "second-agent" }],
    });
    const creation = deferred<void>();
    mutations.createAgent.mockReturnValue(creation.promise);
    await renderLibrary({ tab: "agent" });
    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newAgent"),
    );
    await act(async () => create!.click());
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="submit-agent"]')!.click(),
    );

    await act(async () => container.querySelectorAll<HTMLTableRowElement>("tbody tr")[1].click());
    expect(router.search.sel).toBe("agent:second-agent");

    api.listAgents.mockResolvedValue({
      agents: [{ name: "first-agent" }, { name: "second-agent" }, { name: "created-agent" }],
    });
    creation.resolve();
    await flush();
    expect(router.search.sel).toBe("agent:second-agent");
  });

  it("keeps a row picked while a same-tab playbook clone is pending", async () => {
    api.listBuiltinPlaybooks.mockResolvedValue({ playbooks: [{ name: "bundled" }] });
    api.listPlaybooks.mockResolvedValue({ playbooks: [{ name: "own" }] });
    const clone = deferred<void>();
    mutations.clonePlaybook.mockReturnValue(clone.promise);
    await renderLibrary({ tab: "playbook", sel: "playbook:builtin:bundled" });
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="clone-playbook"]')!.click(),
    );

    await act(async () => container.querySelectorAll<HTMLTableRowElement>("tbody tr")[1].click());
    expect(router.search.sel).toBe("playbook:custom:own");

    clone.resolve();
    await flush();
    expect(router.search.sel).toBe("playbook:custom:own");
  });

  it("refetches a failed source when its tab is revisited", async () => {
    api.listSkills.mockRejectedValueOnce(new Error("skill catalog unavailable"));
    await renderLibrary({ tab: "skill" });
    expect(container.querySelector('[role="alert"]')).not.toBeNull();

    api.listSkills.mockResolvedValue({ skills: [{ name: "alpha-skill" }] });
    await clickTab("filterAgent");
    await clickTab("filterSkill");
    expect(api.listSkills).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.querySelector("tbody")?.textContent).toContain("alpha-skill");
  });

  it("drops a failed banner while its retry is still in flight", async () => {
    api.listSkills.mockRejectedValueOnce(new Error("skill catalog unavailable"));
    await renderLibrary({ tab: "skill" });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("loadError");

    // The revisit starts the source's request but does not settle it: the
    // failed state must already be gone rather than lingering behind the
    // banner until the response arrives.
    const retry = deferred<{ skills: { name: string }[] }>();
    api.listSkills.mockReturnValue(retry.promise);
    await clickTab("filterAgent");
    await clickTab("filterSkill");
    expect(api.listSkills).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[role="alert"]')).toBeNull();

    retry.resolve({ skills: [{ name: "alpha-skill" }] });
    await flush();
    expect(container.querySelector("tbody")?.textContent).toContain("alpha-skill");
  });

  it("resets the detail pane when the tab changes", async () => {
    api.listAgents.mockResolvedValue({ agents: [{ name: "first-agent" }] });
    await renderLibrary({ tab: "agent" });
    await act(async () => container.querySelectorAll<HTMLTableRowElement>("tbody tr")[0].click());
    expect(
      container.querySelector('[data-testid="split-pane"]')?.getAttribute("data-detail-active"),
    ).toBe("true");

    // The Skill tab has nothing to select, so no selection keeps its detail
    // pane up: the collapsed layout must fall back to the list.
    api.listSkills.mockResolvedValue({ skills: [] });
    await clickTab("filterSkill");
    expect(
      container.querySelector('[data-testid="split-pane"]')?.getAttribute("data-detail-active"),
    ).toBe("false");
  });

  it("does not reopen a created workflow after its detail deletes it", async () => {
    const creation = deferred<void>();
    mutations.createWorkflow.mockReturnValue(creation.promise);
    await renderLibrary({ tab: "all" });
    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newWorkflow"),
    );
    expect(create).toBeDefined();
    await act(async () => create!.click());
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="submit-workflow"]')!.click(),
    );
    creation.resolve();
    await flush();
    expect(router.search.sel).toBe("workflow:created-flow");
    expect(container.querySelector('[data-testid="workflow-detail"]')?.textContent).toBe(
      "created-flow-id",
    );

    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="delete-workflow"]')!.click(),
    );
    await flush();
    expect(router.search.sel).toBeUndefined();

    // The created workflow is gone from the catalog and its detail was
    // deleted, so returning to its old selection must not reopen the pane.
    await renderLibrary({ tab: "all", sel: "workflow:created-flow" });
    expect(container.querySelector('[data-testid="workflow-detail"]')).toBeNull();
  });

  it("clears the workflow selection and its row when the detail deletes it", async () => {
    api.listWorkflowDefs.mockResolvedValue([{ id: "workflow-id", name: "saved-flow" }]);
    await renderLibrary({ sel: "workflow:saved-flow" });
    expect(container.querySelector('[data-testid="workflow-detail"]')?.textContent).toBe(
      "workflow-id",
    );
    api.listWorkflowDefs.mockResolvedValue([]);
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="delete-workflow"]')!.click(),
    );
    await flush();
    expect(router.search.sel).toBeUndefined();
    expect(container.querySelector('[data-testid="workflow-detail"]')).toBeNull();

    // The deleted workflow's catalog was dropped too, so a fresh deep link
    // fetches it again instead of resolving the stale cached row.
    await renderLibrary({ sel: "workflow:saved-flow" });
    expect(api.listWorkflowDefs).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[data-testid="workflow-detail"]')).toBeNull();
  });
  it("refetches the agent tab when a pending list answers after a create resolved elsewhere", async () => {
    const listed = deferred<{ agents: { name: string }[] }>();
    api.listAgents.mockReturnValue(listed.promise);
    const creation = deferred<void>();
    mutations.createAgent.mockReturnValue(creation.promise);
    await renderLibrary({ tab: "agent" });
    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newAgent"),
    );
    expect(create).toBeDefined();
    await act(async () => create!.click());
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="submit-agent"]')!.click(),
    );
    expect(mutations.createAgent).toHaveBeenCalledOnce();

    await clickTab("filterSkill");
    api.listAgents.mockResolvedValue({
      agents: [{ name: "existing-agent" }, { name: "created-agent" }],
    });
    creation.resolve();
    await flush();

    // The list request issued before the create answers now with the rows it
    // read before it; that stale answer must not stand in for the refreshed
    // catalog once the viewer returns.
    listed.resolve({ agents: [{ name: "existing-agent" }] });
    await flush();

    await clickTab("filterAgent");
    expect(api.listAgents).toHaveBeenCalledTimes(2);
    expect(container.querySelector("tbody")?.textContent).toContain("created-agent");
  });
  it("keeps a deep-linked row's pane open while its catalog loads", async () => {
    const listed = deferred<{ agents: { name: string }[] }>();
    api.listAgents.mockReturnValue(listed.promise);
    await renderLibrary({ tab: "agent", sel: "agent:first-agent" });
    expect(detailActiveAttr()).toBe("true");
    expect(container.querySelector('[data-testid="agent-detail"]')?.textContent).toBe(
      "first-agent",
    );

    listed.resolve({ agents: [{ name: "first-agent" }] });
    await flush();
    expect(detailActiveAttr()).toBe("true");
  });

  it("keeps a deep-linked row's pane open while its failed catalog is retried", async () => {
    const listed = deferred<{ agents: { name: string }[] }>();
    api.listAgents.mockRejectedValueOnce(new Error("down")).mockReturnValueOnce(listed.promise);
    await renderLibrary({ tab: "agent", sel: "agent:first-agent" });
    expect(detailActiveAttr()).toBe("true");

    await act(async () => retry().click());
    expect(api.listAgents).toHaveBeenCalledTimes(2);
    expect(detailActiveAttr()).toBe("true");

    listed.resolve({ agents: [{ name: "first-agent" }] });
    await flush();
    expect(detailActiveAttr()).toBe("true");
  });

  it.each([[[]], [[{ name: "existing-agent" }]]])(
    "shows a created agent while the reload that lists it is in flight (catalog %j)",
    async (before) => {
      const reloaded = deferred<{ agents: { name: string }[] }>();
      api.listAgents
        .mockResolvedValueOnce({ agents: before })
        .mockReturnValueOnce(reloaded.promise);
      await renderLibrary({ tab: "agent" });
      const create = [...container.querySelectorAll("button")].find((button) =>
        button.textContent?.includes("newAgent"),
      );
      await act(async () => create!.click());
      await act(async () =>
        container.querySelector<HTMLButtonElement>('[data-testid="submit-agent"]')!.click(),
      );
      await flush();
      expect(api.listAgents).toHaveBeenCalledTimes(2);
      expect(router.search).toEqual({ tab: "agent", sel: "agent:created-agent" });
      expect(detailActiveAttr()).toBe("true");
      expect(container.querySelector('[data-testid="agent-detail"]')?.textContent).toBe(
        "created-agent",
      );

      reloaded.resolve({ agents: [...before, { name: "created-agent" }] });
      await flush();
      expect(detailActiveAttr()).toBe("true");
      expect(container.querySelector('[data-testid="agent-detail"]')?.textContent).toBe(
        "created-agent",
      );
    },
  );

  it("returns to the catalog when the reload after a create does not list the row", async () => {
    const reloaded = deferred<{ agents: { name: string }[] }>();
    api.listAgents
      .mockResolvedValueOnce({ agents: [{ name: "existing-agent" }] })
      .mockReturnValueOnce(reloaded.promise);
    await renderLibrary({ tab: "agent" });
    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newAgent"),
    );
    await act(async () => create!.click());
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="submit-agent"]')!.click(),
    );
    await flush();
    expect(detailActiveAttr()).toBe("true");

    reloaded.resolve({ agents: [{ name: "existing-agent" }] });
    await flush();
    expect(router.search).toEqual({ tab: "agent", sel: "agent:created-agent" });
    expect(detailActiveAttr()).toBe("false");
    expect(container.querySelector('[data-testid="agent-detail"]')?.textContent).toBe(
      "existing-agent",
    );
  });

  it("shows a cloned playbook while the reload that lists it is in flight", async () => {
    const reloaded = deferred<{ playbooks: { name: string }[] }>();
    api.listBuiltinPlaybooks.mockResolvedValue({ playbooks: [{ name: "bundled" }] });
    api.listPlaybooks
      .mockResolvedValueOnce({ playbooks: [] })
      .mockReturnValueOnce(reloaded.promise);
    await renderLibrary({ tab: "playbook", sel: "playbook:builtin:bundled" });
    expect(detailActiveAttr()).toBe("true");
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="clone-playbook"]')!.click(),
    );
    await flush();
    expect(api.listPlaybooks).toHaveBeenCalledTimes(2);
    expect(router.search).toEqual({ tab: "playbook", sel: "playbook:custom:cloned-playbook" });
    expect(detailActiveAttr()).toBe("true");
    expect(container.querySelector('[data-testid="playbook-detail"]')?.textContent).toContain(
      "cloned-playbook",
    );

    reloaded.resolve({ playbooks: [{ name: "cloned-playbook" }] });
    await flush();
    expect(detailActiveAttr()).toBe("true");
    expect(
      container.querySelector('[data-testid="playbook-detail"]')?.getAttribute("data-builtin"),
    ).toBe("false");
  });

  it("returns to the catalog when the address moves from a clicked row to one the tab lacks", async () => {
    api.listAgents.mockResolvedValue({
      agents: [{ name: "first-agent" }, { name: "second-agent" }],
    });
    await renderLibrary({ tab: "agent" });
    await act(async () => container.querySelector<HTMLTableRowElement>("tbody tr")!.click());
    expect(detailActiveAttr()).toBe("true");

    await act(async () =>
      router.navigate({ search: () => ({ tab: "agent", sel: "agent:missing-agent" }) }),
    );
    await flush();
    expect(detailActiveAttr()).toBe("false");
  });

  it("keeps the create form open when the router commits its address after the form renders", async () => {
    api.listAgents.mockResolvedValue({ agents: [{ name: "first-agent" }] });
    await renderLibrary({ tab: "agent", sel: "agent:first-agent" });
    // The router commits a navigation after the click's own render, as it does
    // when it loads the route before updating the address.
    const commits: (() => void)[] = [];
    router.navigate.mockImplementation(
      ({ search }: { search: (previous: typeof router.search) => typeof router.search }) => {
        commits.push(() => {
          router.search = search(router.search);
          root.render(<LibraryPage />);
        });
        return Promise.resolve();
      },
    );
    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newAgent"),
    );
    await act(async () => create!.click());
    expect(container.querySelector('[data-testid="submit-agent"]')).not.toBeNull();
    expect(commits).toHaveLength(1);

    await act(async () => commits.shift()!());
    await flush();
    expect(router.search).toEqual({ tab: "agent" });
    expect(container.querySelector('[data-testid="submit-agent"]')).not.toBeNull();
    expect(detailActiveAttr()).toBe("true");
  });

  it("shows an agent created after a delete resolved behind its create form", async () => {
    const deletion = deferred<void>();
    mutations.deleteAgent.mockReturnValue(deletion.promise);
    const reloaded = deferred<{ agents: { name: string }[] }>();
    api.listAgents
      .mockResolvedValueOnce({ agents: [{ name: "first-agent" }] })
      .mockResolvedValueOnce({ agents: [] })
      .mockReturnValueOnce(reloaded.promise);
    await renderLibrary({ tab: "agent", sel: "agent:first-agent" });
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="delete-agent"]')!.click(),
    );
    const create = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("newAgent"),
    );
    await act(async () => create!.click());
    deletion.resolve();
    await flush();
    expect(container.querySelector('[data-testid="submit-agent"]')).not.toBeNull();

    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="submit-agent"]')!.click(),
    );
    await flush();
    expect(router.search).toEqual({ tab: "agent", sel: "agent:created-agent" });
    expect(detailActiveAttr()).toBe("true");
    expect(container.querySelector('[data-testid="agent-detail"]')?.textContent).toBe(
      "created-agent",
    );
  });
});
