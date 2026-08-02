/**
 * McpServerDetail — env-key deletion contract.
 *
 * The UI copy tells the operator that deleting a `KEY=` line removes that
 * key (see the hint text under the env textarea). This exercises the
 * component's own save path end to end: load a server with existing env
 * keys, edit the textarea to drop one line, save, and assert the exact
 * patch handed to the API call — the thing the backend's merge semantics
 * actually see.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { McpServerSummary } from "@/lib/api";

const api = vi.hoisted(() => ({
  getMcpServer: vi.fn(),
  updateMcpServer: vi.fn(),
  deleteMcpServer: vi.fn(),
  setMcpServerEnabled: vi.fn(),
  checkMcpServer: vi.fn(),
  validateMcpServer: vi.fn(),
  registerMcpServer: vi.fn(),
}));

vi.mock("@/lib/api", () => api);

const { McpServerDetail } = await import("./McpServerDetail");

function server(overrides: Partial<McpServerSummary> = {}): McpServerSummary {
  return {
    name: "myserver",
    transport: "stdio",
    command: "python3",
    args: ["-m", "some_mcp_server"],
    env_keys: ["API_KEY", "OTHER_VAR"],
    enabled: true,
    created_at: 1,
    updated_at: 1,
    last_check: null,
    ...overrides,
  };
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("McpServerDetail — env deletion", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    Object.values(api).forEach((fn) => fn.mockReset());
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function mount(initial = server()) {
    api.getMcpServer.mockResolvedValue(initial);
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <McpServerDetail name="myserver" />
        </IntlProvider>,
      );
    });
    await flush();
  }

  it("deleting an env line serializes an explicit null for that key, not omission", async () => {
    await mount();

    const editButton = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === "Edit",
    )!;
    await act(async () => {
      editButton.click();
    });

    const textarea = [...container.querySelectorAll("textarea")].find((t) =>
      t.value.includes("API_KEY="),
    )!;
    expect(textarea.value).toBe("API_KEY=\nOTHER_VAR=");

    // Drop the OTHER_VAR line entirely — this is the operator's "remove
    // this key" action. API_KEY is left untouched (still a bare `KEY=`).
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(
        textarea,
        "API_KEY=",
      );
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });

    api.updateMcpServer.mockResolvedValue(server({ env_keys: ["API_KEY"] }));

    const saveButton = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === "Save",
    )!;
    await act(async () => {
      saveButton.click();
    });
    await flush();

    expect(api.updateMcpServer).toHaveBeenCalledTimes(1);
    const [, patch] = api.updateMcpServer.mock.calls[0];
    expect(patch.env).toEqual({ OTHER_VAR: null });
  });

  it("leaving all env lines untouched sends no env patch at all (unrelated field edits don't wipe secrets)", async () => {
    await mount();

    const editButton = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === "Edit",
    )!;
    await act(async () => {
      editButton.click();
    });

    const commandInput = container.querySelector("input[type=text]") as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(
        commandInput,
        "python3.11",
      );
      commandInput.dispatchEvent(new Event("input", { bubbles: true }));
    });

    api.updateMcpServer.mockResolvedValue(server({ command: "python3.11" }));

    const saveButton = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === "Save",
    )!;
    await act(async () => {
      saveButton.click();
    });
    await flush();

    const [, patch] = api.updateMcpServer.mock.calls[0];
    expect(patch.env).toBeUndefined();
    expect(patch.command).toBe("python3.11");
  });

  it("clearing the args editor sends an explicit empty list, not an omission", async () => {
    await mount(server({ args: ["-m", "some_mcp_server"], timeout: 30 }));

    const editButton = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === "Edit",
    )!;
    await act(async () => {
      editButton.click();
    });

    const argsArea = [...container.querySelectorAll("textarea")].find((t) =>
      t.value.includes("some_mcp_server"),
    )!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(
        argsArea,
        "",
      );
      argsArea.dispatchEvent(new Event("input", { bubbles: true }));
    });

    api.updateMcpServer.mockResolvedValue(server({ args: [], timeout: 30 }));

    const saveButton = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === "Save",
    )!;
    await act(async () => {
      saveButton.click();
    });
    await flush();

    const [, patch] = api.updateMcpServer.mock.calls[0];
    // Omitting args would leave the old ones in place: the server preserves
    // every key a patch does not mention.
    expect(patch.args).toEqual([]);
  });

  it("clearing the timeout sends an explicit null, not an omission", async () => {
    await mount(server({ timeout: 30 }));

    const editButton = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === "Edit",
    )!;
    await act(async () => {
      editButton.click();
    });

    const timeoutInput = [...container.querySelectorAll("input")].find(
      (i) => i.value === "30",
    ) as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(
        timeoutInput,
        "",
      );
      timeoutInput.dispatchEvent(new Event("input", { bubbles: true }));
    });

    // The backend omits an absent timeout rather than returning null.
    api.updateMcpServer.mockResolvedValue(server());

    const saveButton = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === "Save",
    )!;
    await act(async () => {
      saveButton.click();
    });
    await flush();

    const [, patch] = api.updateMcpServer.mock.calls[0];
    expect(patch.timeout).toBeNull();
  });
});
