/**
 * Library detail views — Back while loading or failed.
 *
 * In the collapsed Library layout a detail view replaces the catalog, so its
 * loading, error and not-found states must offer Back as its loaded state
 * does; otherwise a slow or failed request leaves no way back to the list.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import { ToastProvider } from "@/components/ui/Toast";

const api = vi.hoisted(() => ({
  checkMcpServer: vi.fn(),
  deleteAgent: vi.fn(),
  deleteMcpServer: vi.fn(),
  getAgent: vi.fn(),
  getBuiltinPlaybookRaw: vi.fn(),
  getCasts: vi.fn(),
  getDefinition: vi.fn(),
  getDefinitionVersion: vi.fn(),
  getHookLibrary: vi.fn(),
  getMcpServer: vi.fn(),
  getPlugin: vi.fn(),
  getPluginAgent: vi.fn(),
  getPluginSkill: vi.fn(),
  getSkill: vi.fn(),
  getWorkerRaw: vi.fn(),
  installBuiltinPlaybook: vi.fn(),
  launchPlaybook: vi.fn(),
  listRuns: vi.fn(),
  rawToDeclarative: vi.fn(),
  registerMcpServer: vi.fn(),
  rollbackDefinition: vi.fn(),
  saveDefinition: vi.fn(),
  setMcpServerEnabled: vi.fn(),
  updateAgent: vi.fn(),
  updateMcpServer: vi.fn(),
  validateMcpServer: vi.fn(),
  validateSkill: vi.fn(),
}));

vi.mock("@/lib/api", () => api);
vi.mock("@/lib/useInvocationStats", () => ({
  useInvocationStats: () => ({ stats: null, loading: true, error: false, retry: () => {} }),
  formatInvocationAge: () => "",
}));

const { AgentDetail } = await import("./AgentDetail");
const { McpServerDetail } = await import("./McpServerDetail");
const { PlaybookTemplateDetail } = await import("./PlaybookTemplateDetail");
const { SkillDetail } = await import("./SkillDetail");
const { PluginDetail } = await import("./PluginDetail");

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

const views: [string, (onBack: () => void) => ReactNode][] = [
  [
    "AgentDetail",
    (onBack) => (
      <AgentDetail agent={{ name: "an-agent", provider: "", model: "" }} onBack={onBack} />
    ),
  ],
  ["McpServerDetail", (onBack) => <McpServerDetail name="a-server" onBack={onBack} />],
  [
    "PlaybookTemplateDetail",
    (onBack) => <PlaybookTemplateDetail name="a-playbook" isBuiltin={false} onBack={onBack} />,
  ],
  ["SkillDetail", (onBack) => <SkillDetail name="a-skill" onBack={onBack} />],
  ["PluginDetail", (onBack) => <PluginDetail name="a-plugin" onBack={onBack} />],
];

describe("Library detail views offer Back before they load", () => {
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

  async function mount(view: (onBack: () => void) => ReactNode, onBack: () => void) {
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <ToastProvider>{view(onBack)}</ToastProvider>
        </IntlProvider>,
      );
    });
    await flush();
  }

  function backButton() {
    return [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("Back"),
    );
  }

  it.each(views)("%s offers Back while it loads", async (_name, view) => {
    Object.values(api).forEach((fn) => fn.mockReturnValue(new Promise(() => {})));
    const onBack = vi.fn();
    await mount(view, onBack);

    expect(container.textContent).toContain("Loading");
    const back = backButton();
    expect(back).toBeDefined();
    await act(async () => back!.click());
    expect(onBack).toHaveBeenCalledOnce();
  });

  it.each(views)("%s offers Back after its request fails", async (_name, view) => {
    Object.values(api).forEach((fn) => fn.mockRejectedValue(new Error("unavailable")));
    const onBack = vi.fn();
    await mount(view, onBack);

    expect(container.textContent).not.toContain("Loading");
    const back = backButton();
    expect(back).toBeDefined();
    await act(async () => back!.click());
    expect(onBack).toHaveBeenCalledOnce();
  });
});
