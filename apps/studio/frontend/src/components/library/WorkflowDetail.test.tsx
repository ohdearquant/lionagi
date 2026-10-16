import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";

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
});
