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

const { CreateWorkflowPanel } = await import("./WorkflowDetail");

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
