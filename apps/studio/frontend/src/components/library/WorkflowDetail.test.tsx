import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { WorkflowSpec } from "@/lib/api";

const api = vi.hoisted(() => ({
  createWorkflowDef: vi.fn(),
  deleteWorkflowDef: vi.fn(),
  getWorkflowDef: vi.fn(),
  listEngineDefs: vi.fn(),
  updateWorkflowDef: vi.fn(),
}));

vi.mock("@/lib/api", () => api);

const { CreateWorkflowPanel, WorkflowDetail } = await import("./WorkflowDetail");

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("CreateWorkflowPanel", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    Object.values(api).forEach((mock) => mock.mockReset());
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it("passes the created workflow identity to the parent instead of replacing it with the name", async () => {
    const created = {
      id: "wf_01JZ9K8Y5M",
      name: "Réview flow: alpha/beta?",
      created_at: 42,
    };
    api.createWorkflowDef.mockResolvedValue(created);
    const onCreated = vi.fn();

    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <CreateWorkflowPanel onCreated={onCreated} onCancel={vi.fn()} />
        </IntlProvider>,
      );
    });

    const nameInput = container.querySelector("input[type=text]") as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(
        nameInput,
        `  ${created.name}  `,
      );
      nameInput.dispatchEvent(new Event("input", { bubbles: true }));
    });

    const createButton = [...container.querySelectorAll("button")].find(
      (button) => button.textContent === "Create",
    );
    expect(createButton).toBeDefined();

    await act(async () => {
      createButton?.click();
    });
    await flush();

    expect(api.createWorkflowDef).toHaveBeenCalledWith(
      expect.objectContaining({ name: created.name }),
    );
    expect(onCreated).toHaveBeenCalledWith(created);
  });
});

// A workflow's node editor picks engine definitions, so the detail fetches the
// engine catalog itself. That is a second, independent source: a failure or a
// pending response there must not replace the workflow that already loaded.
describe("WorkflowDetail — engine catalog", () => {
  let container: HTMLDivElement;
  let root: Root;

  // jsdom has no ResizeObserver; the canvas asks for one on mount. Observing
  // nothing leaves the editor in its default layout.
  class StubObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  }

  const savedWorkflow = {
    id: "wf_01JZ9K8Y5M",
    name: "saved-flow",
    description: null,
    spec_json: null,
    created_at: 1,
    updated_at: 1,
  };

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("ResizeObserver", StubObserver);
    Object.values(api).forEach((mock) => mock.mockReset());
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function renderDetail() {
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <WorkflowDetail id={savedWorkflow.id} />
        </IntlProvider>,
      );
    });
    await flush();
  }

  async function renderDetailWith(id: string) {
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <WorkflowDetail id={id} />
        </IntlProvider>,
      );
    });
    await flush();
  }

  async function renderDetailWithDelete(onDeleted: () => void) {
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <WorkflowDetail id={savedWorkflow.id} onDeleted={onDeleted} />
        </IntlProvider>,
      );
    });
    await flush();
  }

  it("reports a completed delete to the list instead of only collapsing the pane", async () => {
    api.getWorkflowDef.mockResolvedValue(savedWorkflow);
    api.listEngineDefs.mockResolvedValue([]);
    api.deleteWorkflowDef.mockResolvedValue({ ok: true });
    const onDeleted = vi.fn();
    await renderDetailWithDelete(onDeleted);

    const remove = [...container.querySelectorAll("button")].find(
      (element) => element.textContent === "Delete",
    );
    expect(remove).toBeDefined();
    // The first click arms the confirmation; the second performs the delete.
    await act(async () => remove!.click());
    const confirm = [...container.querySelectorAll("button")].find(
      (element) => element.textContent === "Confirm delete",
    );
    expect(confirm).toBeDefined();
    await act(async () => confirm!.click());
    await flush();

    expect(api.deleteWorkflowDef).toHaveBeenCalledOnce();
    expect(onDeleted).toHaveBeenCalledOnce();
  });

  it("keeps the editor and retries only the engine catalog when that source fails", async () => {
    api.getWorkflowDef.mockResolvedValue(savedWorkflow);
    api.listEngineDefs.mockRejectedValueOnce(new Error("engine catalog unavailable"));
    await renderDetail();

    expect(api.getWorkflowDef).toHaveBeenCalledOnce();
    expect(api.listEngineDefs).toHaveBeenCalledOnce();
    expect(container.textContent).toContain("Workflow Editor");
    expect(container.textContent).not.toContain("Workflow not found");

    const alert = container.querySelector('[role="alert"]');
    expect(alert).not.toBeNull();
    expect(alert?.textContent).toContain("Failed to load");

    api.listEngineDefs.mockResolvedValue([
      { id: "engine-id", name: "saved-engine", kind: "local" },
    ]);
    await act(async () => alert!.querySelector<HTMLButtonElement>("button")!.click());
    await flush();

    expect(api.listEngineDefs).toHaveBeenCalledTimes(2);
    expect(api.getWorkflowDef).toHaveBeenCalledOnce();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("keeps the editor with a loading notice while the engine catalog is still pending", async () => {
    api.getWorkflowDef.mockResolvedValue(savedWorkflow);
    api.listEngineDefs.mockReturnValue(new Promise(() => {}));
    await renderDetail();

    expect(container.textContent).toContain("Workflow Editor");
    expect(container.textContent).not.toContain("Workflow not found");
    expect(container.querySelector('[role="status"]')).not.toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  // An absent catalog cannot testify that an engine id is unknown. Until the
  // catalog resolves, rule 4 must stay out of the way so a workflow with an
  // engine node remains editable.
  const engineSpec: WorkflowSpec = {
    version: 1,
    nodes: [
      { id: "n1", kind: "input", label: "Input", pos: { x: 64, y: 64 } },
      {
        id: "n2",
        kind: "engine",
        label: "Engine",
        pos: { x: 320, y: 64 },
        config: { engine_def_id: "engine-id" },
      },
    ],
    edges: [{ id: "e1", from: "n1", to: "n2" }],
    inputs: [],
    outputs: [],
  };

  function saveButton() {
    const button = [...container.querySelectorAll("button")].find(
      (element) => element.textContent === "Save",
    );
    expect(button).toBeDefined();
    return button as HTMLButtonElement;
  }

  it("keeps Save usable while an engine node's catalog is still pending", async () => {
    api.getWorkflowDef.mockResolvedValue({ ...savedWorkflow, spec_json: engineSpec });
    api.listEngineDefs.mockReturnValue(new Promise(() => {}));
    await renderDetail();

    expect(saveButton().disabled).toBe(false);
    expect(container.textContent).not.toContain("issue");
  });

  it("keeps Save usable when an engine node's catalog fails", async () => {
    api.getWorkflowDef.mockResolvedValue({ ...savedWorkflow, spec_json: engineSpec });
    api.listEngineDefs.mockRejectedValueOnce(new Error("engine catalog unavailable"));
    await renderDetail();

    expect(saveButton().disabled).toBe(false);
    expect(container.textContent).not.toContain("issue");
  });

  it("does not write a save that resolved after the detail changed workflows", async () => {
    const workflowA = {
      id: "wf-a",
      name: "alpha-workflow",
      description: null,
      spec_json: null,
      created_at: 1,
      updated_at: 1,
    };
    const workflowB = {
      id: "wf-b",
      name: "beta-workflow",
      description: null,
      spec_json: null,
      created_at: 2,
      updated_at: 2,
    };
    api.getWorkflowDef.mockImplementation(async (id: string) =>
      id === "wf-b" ? workflowB : workflowA,
    );
    api.listEngineDefs.mockResolvedValue([]);
    await renderDetailWith("wf-a");
    expect(container.textContent).toContain("alpha-workflow");

    const update = deferred<void>();
    api.updateWorkflowDef.mockReturnValue(update.promise);
    await act(async () => saveButton().click());
    expect(api.updateWorkflowDef).toHaveBeenCalledOnce();

    await renderDetailWith("wf-b");
    expect(container.textContent).toContain("beta-workflow");

    update.resolve();
    await flush();
    expect(container.textContent).toContain("beta-workflow");
    expect(container.textContent).not.toContain("alpha-workflow");
  });
});
