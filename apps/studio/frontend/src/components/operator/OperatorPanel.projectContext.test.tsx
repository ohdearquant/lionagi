import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { SessionDetail } from "@/lib/api";
import { validateFleetSearch } from "@/routes/fleet";

const api = vi.hoisted(() => ({
  ApiError: class extends Error {},
  acknowledgeOperatorEffect: vi.fn(),
  cancelOperatorRequest: vi.fn(),
  createOperatorConversation: vi.fn(),
  decideOperatorProposal: vi.fn(),
  fetchOperatorModelCatalog: vi.fn(),
  forkOperatorConversation: vi.fn(),
  getOperatorConversation: vi.fn(),
  listOperatorConversations: vi.fn(),
  reportOperatorView: vi.fn(),
  streamOperatorConversation: vi.fn(),
  submitOperatorTurn: vi.fn(),
  updateOperatorConversation: vi.fn(),
  getRunFile: vi.fn(),
  getSession: vi.fn(),
  getInvocation: vi.fn(),
  streamSession: vi.fn(),
  streamSignals: vi.fn(),
}));
const router = vi.hoisted(() => ({
  navigate: vi.fn(),
  location: { pathname: "/fleet", search: {} as Record<string, unknown> },
}));

vi.mock("@/lib/api", () => api);
vi.mock("@tanstack/react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@tanstack/react-router")>()),
  Link: ({ children }: { children?: React.ReactNode }) => <a href="/fleet">{children}</a>,
  useLocation: () => router.location,
  useSearch: (options: { from: string; shouldThrow: boolean }) => {
    expect(options).toEqual({ from: "/fleet", shouldThrow: false });
    return router.location.pathname === "/fleet"
      ? validateFleetSearch(router.location.search)
      : undefined;
  },
  useNavigate: () => router.navigate,
}));
vi.mock("@/components/shell/IconRail", () => ({ default: () => null }));
vi.mock("@/components/shell/TopBar", () => ({ default: () => null }));
vi.mock("@/components/shell/StatusFooter", () => ({ default: () => null }));
vi.mock("@/components/shell/CommandPalette", () => ({ default: () => null }));
vi.mock("@/components/history/ResumeRun", () => ({ default: () => null }));

const { default: AppShell } = await import("@/components/shell/AppShell");
const { default: RunDetail } = await import("@/components/history/RunDetail");

function session(id: string, project?: string | null): SessionDetail {
  return { id, name: id, project, created_at: 1, updated_at: 1, status: "completed", branches: [] };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("Operator project context", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.resetAllMocks();
    const storage = new Map([["studio:operator-conversation", "conversation-1"]]);
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => storage.set(key, value),
        removeItem: (key: string) => storage.delete(key),
      },
    });
    router.location = { pathname: "/fleet", search: {} };
    api.getOperatorConversation.mockResolvedValue({
      conversation: { id: "conversation-1", status: "active", activeRequestId: null },
      frames: [],
    });
    api.listOperatorConversations.mockResolvedValue([
      { id: "conversation-1", status: "active", activeRequestId: null },
    ]);
    api.fetchOperatorModelCatalog.mockResolvedValue({ models: [] });
    api.reportOperatorView.mockResolvedValue(undefined);
    api.submitOperatorTurn.mockResolvedValue({ requestId: "request-1" });
    api.streamOperatorConversation.mockReturnValue(vi.fn());
    api.streamSession.mockReturnValue(vi.fn());
    api.streamSignals.mockReturnValue(vi.fn());
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    Element.prototype.scrollIntoView = vi.fn();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  async function render(id?: string) {
    await act(async () => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <AppShell onLocaleChange={vi.fn()}>{id ? <RunDetail id={id} /> : null}</AppShell>
        </IntlProvider>,
      );
    });
  }

  async function viewProject() {
    await act(async () => vi.advanceTimersByTime(150));
    expect(api.reportOperatorView).toHaveBeenCalled();
    return api.reportOperatorView.mock.lastCall?.[1].project;
  }

  async function sendProject() {
    const textarea = container.querySelector("textarea")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(
        textarea,
        "Show run progress",
      );
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const send = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.startsWith("Send"),
    );
    expect(send).toBeDefined();
    await act(async () => send?.click());
    expect(api.submitOperatorTurn).toHaveBeenCalledOnce();
    return api.submitOperatorTurn.mock.lastCall?.[1].context.project;
  }

  it("uses an existing detail response for a selected run, overriding the list filter", async () => {
    router.location.search = { s: "selected", project: "list-project" };
    api.getSession.mockResolvedValue(session("selected", "selected-project"));
    await render("selected");

    expect(await viewProject()).toBe("selected-project");
    expect(await sendProject()).toBe("selected-project");
    expect(api.getSession).toHaveBeenCalledExactlyOnceWith("selected");
  });

  it("reports a project when detail resolves without a route change", async () => {
    const detail = deferred<SessionDetail>();
    router.location.search = { s: "selected", project: "list-project" };
    api.getSession.mockReturnValue(detail.promise);
    await render("selected");
    expect(await viewProject()).toBeUndefined();

    await act(async () => detail.resolve(session("selected", "selected-project")));
    expect(await viewProject()).toBe("selected-project");
    expect(await sendProject()).toBe("selected-project");
  });

  it.each([null, undefined])("keeps a selected run with project %s unscoped", async (project) => {
    router.location.search = { s: "selected", project: "list-project" };
    api.getSession.mockResolvedValue(session("selected", project));
    await render("selected");
    expect(await viewProject()).toBeUndefined();
    expect(await sendProject()).toBeUndefined();
  });

  it.each([{ project: "list-project" }, { project: ["", "list-project", "other-project"] }])(
    "uses a validated project filter %j when the Fleet list has no selection",
    async ({ project }) => {
      router.location.search = { project };
      await render();
      expect(await viewProject()).toBe("list-project");
      expect(await sendProject()).toBe("list-project");
      expect(api.getSession).not.toHaveBeenCalled();
    },
  );

  it.each([
    { project: { name: "unvalidated" } },
    { project: "" },
    { project: "list-project", project_null: true },
    { project: "list-project", project_null: "true" },
    { project: "list-project", invocation: "pending-invocation" },
  ])("does not infer scope from an ambiguous list filter %j", async (search) => {
    router.location.search = search;
    await render();
    expect(await viewProject()).toBeUndefined();
    expect(await sendProject()).toBeUndefined();
  });

  it("uses the validated selection from an array-valued link", async () => {
    router.location.search = { s: ["", "selected", "other"], project: "list-project" };
    api.getSession.mockResolvedValue(session("selected", "selected-project"));
    await render("selected");
    expect(await viewProject()).toBe("selected-project");
    expect(await sendProject()).toBe("selected-project");
  });

  it("ignores a detail response whose identity differs from the selection", async () => {
    router.location.search = { s: "selected", project: "list-project" };
    api.getSession.mockResolvedValue(session("different", "different-project"));
    await render("selected");
    expect(await viewProject()).toBeUndefined();
    expect(await sendProject()).toBeUndefined();
  });

  it("withholds the previous detail's project while a new selection loads", async () => {
    const selected = deferred<SessionDetail>();
    api.getSession
      .mockResolvedValueOnce(session("previous", "previous-project"))
      .mockReturnValueOnce(selected.promise);
    router.location.search = { s: "previous" };
    await render("previous");
    expect(await viewProject()).toBe("previous-project");

    router.location = { pathname: "/fleet", search: { s: "selected", project: "list-project" } };
    await render("previous");
    expect(await viewProject()).toBeUndefined();
    await render("selected");
    expect(await viewProject()).toBeUndefined();

    await act(async () => selected.resolve(session("selected", "selected-project")));
    expect(await viewProject()).toBe("selected-project");
    expect(await sendProject()).toBe("selected-project");
  });

  it.each(["resolve", "reject"] as const)(
    "ignores a previous selection's delayed %s",
    async (outcome) => {
      const previous = deferred<SessionDetail>();
      const selected = deferred<SessionDetail>();
      api.getSession.mockReturnValueOnce(previous.promise).mockReturnValueOnce(selected.promise);
      router.location.search = { s: "previous" };
      await render("previous");
      router.location = { pathname: "/fleet", search: { s: "selected", project: "list-project" } };
      await render("selected");
      expect(await viewProject()).toBeUndefined();

      await act(async () => selected.resolve(session("selected", "selected-project")));
      expect(await viewProject()).toBe("selected-project");
      await act(async () => {
        if (outcome === "resolve") previous.resolve(session("previous", "previous-project"));
        else previous.reject(new Error("Previous detail unavailable"));
      });
      expect(await viewProject()).toBe("selected-project");
      expect(await sendProject()).toBe("selected-project");
      expect(api.getSession).toHaveBeenCalledTimes(2);
    },
  );

  it("clears a detail project when selection is removed or the route changes", async () => {
    router.location.search = { s: "selected" };
    api.getSession.mockResolvedValue(session("selected", "selected-project"));
    await render("selected");
    expect(await viewProject()).toBe("selected-project");

    router.location = { pathname: "/fleet", search: {} };
    await render();
    expect(await viewProject()).toBeUndefined();
    router.location = { pathname: "/system", search: { project: "other-project" } };
    await render();
    expect(await viewProject()).toBeUndefined();
    expect(await sendProject()).toBeUndefined();
  });
});
