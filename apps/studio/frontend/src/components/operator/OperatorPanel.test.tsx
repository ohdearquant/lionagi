import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { OperatorFrame } from "@/lib/types";

const api = vi.hoisted(() => ({
  acknowledgeOperatorEffect: vi.fn(),
  cancelOperatorRequest: vi.fn(),
  createOperatorConversation: vi.fn(),
  decideOperatorProposal: vi.fn(),
  getOperatorConversation: vi.fn(),
  listOperatorConversations: vi.fn(),
  streamOperatorConversation: vi.fn(() => vi.fn()),
  submitOperatorTurn: vi.fn(),
  getRunFile: vi.fn(),
}));
const router = vi.hoisted(() => ({ navigate: vi.fn() }));

vi.mock("@/lib/api", () => api);
vi.mock("@tanstack/react-router", () => ({
  Link: ({ children }: { children?: React.ReactNode }) => <a href="/fleet">{children}</a>,
  useLocation: () => ({ pathname: "/", search: {} }),
  useNavigate: () => router.navigate,
}));

const { default: OperatorPanel } = await import("./OperatorPanel");

function textFrame(sequence: number, role: "user" | "assistant", content: string): OperatorFrame {
  return {
    version: 1,
    conversationId: "conversation-1",
    requestId: "request-1",
    sequence,
    type: "text",
    payload: { content, format: "plain", role },
    createdAt: sequence,
  };
}

describe("OperatorPanel", () => {
  let container: HTMLDivElement;
  let root: Root | null;
  const storage = new Map<string, string>();

  beforeEach(() => {
    storage.clear();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => storage.set(key, value),
        removeItem: (key: string) => storage.delete(key),
        clear: () => storage.clear(),
        key: (index: number) => [...storage.keys()][index] ?? null,
        get length() {
          return storage.size;
        },
      },
    });
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    api.getOperatorConversation.mockReset();
    api.listOperatorConversations.mockReset();
    api.listOperatorConversations.mockResolvedValue([]);
    api.decideOperatorProposal.mockReset();
    api.decideOperatorProposal.mockResolvedValue({
      proposalId: "proposal-1",
      status: "succeeded",
    });
    api.acknowledgeOperatorEffect.mockReset();
    api.acknowledgeOperatorEffect.mockResolvedValue({
      effectId: "effect-1",
      status: "applied",
    });
    router.navigate.mockReset();
    router.navigate.mockResolvedValue(undefined);
    document.documentElement.setAttribute("data-theme", "dark");
    api.streamOperatorConversation.mockReset();
    api.streamOperatorConversation.mockReturnValue(vi.fn());
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
    if (root) act(() => root?.unmount());
    root = null;
    container.remove();
    vi.unstubAllGlobals();
  });

  async function mount() {
    await act(async () => {
      root?.render(
        <IntlProvider locale="en" messages={enMessages}>
          <OperatorPanel open onClose={vi.fn()} />
        </IntlProvider>,
      );
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("is an immediate command front door when no conversation exists", async () => {
    await mount();

    expect(container.textContent).toContain("What should we do?");
    expect(container.querySelector("textarea")?.placeholder).toContain("Ask Operator");
    expect(container.querySelector('button[aria-label="Close Operator"]')).not.toBeNull();
    expect(api.listOperatorConversations).toHaveBeenCalledOnce();
    expect(api.getOperatorConversation).not.toHaveBeenCalled();
  });

  it("restores daemon history using only the persisted conversation id", async () => {
    window.localStorage.setItem("studio:operator-conversation", "conversation-1");
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-1",
        title: "Scheduler check",
        status: "active",
        activeRequestId: null,
        updatedAt: 2,
      },
    ]);
    api.getOperatorConversation.mockResolvedValue({
      conversation: {
        id: "conversation-1",
        status: "active",
        activeRequestId: null,
      },
      frames: [
        textFrame(1, "user", "Inspect the scheduler"),
        textFrame(2, "assistant", "The scheduler is healthy."),
      ],
    });

    await mount();

    expect(api.getOperatorConversation).toHaveBeenCalledWith("conversation-1");
    expect(container.textContent).toContain("Inspect the scheduler");
    expect(container.textContent).toContain("The scheduler is healthy.");
    expect(api.streamOperatorConversation).toHaveBeenCalledWith(
      "conversation-1",
      2,
      expect.any(Object),
    );
    expect(window.localStorage.getItem("studio:operator-conversation")).toBe("conversation-1");
    expect(
      [...Array(window.localStorage.length)].map((_, index) => window.localStorage.key(index)),
    ).not.toContain("studio:operator-token");
  });

  it("recovers the latest daemon conversation when there is no cached id", async () => {
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-latest",
        title: "Latest daemon history",
        status: "active",
        activeRequestId: null,
        updatedAt: 20,
      },
      {
        id: "conversation-older",
        title: "Older daemon history",
        status: "active",
        activeRequestId: null,
        updatedAt: 10,
      },
    ]);
    api.getOperatorConversation.mockResolvedValue({
      conversation: {
        id: "conversation-latest",
        title: "Latest daemon history",
        status: "active",
        activeRequestId: null,
      },
      frames: [
        {
          ...textFrame(1, "assistant", "Recovered from the daemon."),
          conversationId: "conversation-latest",
        },
      ],
    });

    await mount();

    expect(api.getOperatorConversation).toHaveBeenCalledWith("conversation-latest");
    expect(container.textContent).toContain("Recovered from the daemon.");
    expect(window.localStorage.getItem("studio:operator-conversation")).toBe("conversation-latest");
    const switcher = container.querySelector('select[aria-label="Operator conversation"]');
    expect(switcher?.querySelectorAll("option")).toHaveLength(3);
    expect(switcher?.textContent).toContain("Older daemon history");
  });

  it("falls back from a stale cached id and keeps earlier daemon history reachable", async () => {
    window.localStorage.setItem("studio:operator-conversation", "deleted-conversation");
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-active",
        title: "Active conversation",
        status: "active",
        activeRequestId: null,
        updatedAt: 20,
      },
      {
        id: "conversation-prior",
        title: "Prior conversation",
        status: "active",
        activeRequestId: null,
        updatedAt: 10,
      },
    ]);
    api.getOperatorConversation.mockImplementation((id: string) =>
      Promise.resolve({
        conversation: { id, title: id, status: "active", activeRequestId: null },
        frames:
          id === "conversation-prior"
            ? [
                {
                  ...textFrame(1, "assistant", "Prior daemon transcript"),
                  conversationId: "conversation-prior",
                },
              ]
            : [],
      }),
    );

    await mount();

    expect(api.getOperatorConversation).toHaveBeenCalledWith("conversation-active");
    expect(window.localStorage.getItem("studio:operator-conversation")).toBe("conversation-active");

    const switcher = container.querySelector(
      'select[aria-label="Operator conversation"]',
    ) as HTMLSelectElement;
    await act(async () => {
      switcher.value = "conversation-prior";
      switcher.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
    });

    expect(api.getOperatorConversation).toHaveBeenCalledWith("conversation-prior");
    expect(container.textContent).toContain("Prior daemon transcript");
    expect(window.localStorage.getItem("studio:operator-conversation")).toBe("conversation-prior");
  });

  it("reveals the bounded proposed command and disables it after a denial", async () => {
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-1",
        title: "Permission review",
        status: "active",
        activeRequestId: null,
      },
    ]);
    api.getOperatorConversation.mockResolvedValue({
      conversation: {
        id: "conversation-1",
        status: "active",
        activeRequestId: null,
      },
      frames: [
        {
          version: 1,
          conversationId: "conversation-1",
          requestId: "request-1",
          sequence: 1,
          type: "proposal",
          payload: {
            proposal: {
              id: "proposal-1",
              command: {
                tool: "Bash",
                arguments: { command: "git status", authorization: "Bearer secret" },
              },
              commandHash: "sha256",
              risk: "execute",
              summary: "Inspect repository state",
              target: {
                kind: "playbook",
                id: "review",
                version: "playbook-fingerprint",
              },
              idempotencyKey: "once",
              expiresAt: 999,
            },
          },
          createdAt: 1,
        },
      ] satisfies OperatorFrame[],
    });

    await mount();

    expect(container.textContent).toContain("Review exact command");
    expect(container.textContent).toContain("Bash");
    expect(container.textContent).toContain("git status");
    expect(container.textContent).toContain("[redacted]");
    expect(container.textContent).not.toContain("Bearer secret");

    const deny = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Deny",
    );
    await act(async () => {
      deny?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });

    expect(api.decideOperatorProposal).toHaveBeenCalledWith(
      "conversation-1",
      "proposal-1",
      "deny",
      "sha256",
      "playbook-fingerprint",
    );
    expect(container.textContent).toContain("Decision recorded");
    expect(
      Array.from(container.querySelectorAll("button")).find(
        (button) => button.textContent === "Allow",
      ),
    ).toBeUndefined();
  });

  it("applies and durably acknowledges a validated theme effect once", async () => {
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-1",
        title: "Theme update",
        status: "active",
        activeRequestId: null,
      },
    ]);
    api.getOperatorConversation.mockResolvedValue({
      conversation: {
        id: "conversation-1",
        status: "active",
        activeRequestId: null,
      },
      frames: [
        {
          version: 1,
          conversationId: "conversation-1",
          requestId: "request-1",
          sequence: 1,
          type: "ui_command",
          payload: {
            effect: { id: "effect-1", kind: "theme", theme: "light" },
          },
          createdAt: 1,
        },
      ] satisfies OperatorFrame[],
    });

    await mount();

    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(api.acknowledgeOperatorEffect).toHaveBeenCalledWith("conversation-1", "effect-1", {
      status: "applied",
      clientRoute: "/",
    });
    expect(
      JSON.parse(window.localStorage.getItem("studio:operator-effects:conversation-1") ?? "[]"),
    ).toContainEqual(["effect-1", { status: "applied", clientRoute: "/" }]);
  });

  it("fails closed without replaying an effect when acknowledgement storage is blocked", async () => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => {
          if (key.startsWith("studio:operator-effects:")) throw new Error("blocked");
          storage.set(key, value);
        },
        removeItem: (key: string) => storage.delete(key),
        clear: () => storage.clear(),
        key: (index: number) => [...storage.keys()][index] ?? null,
        get length() {
          return storage.size;
        },
      },
    });
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-1",
        title: "Theme update",
        status: "active",
        activeRequestId: null,
      },
    ]);
    api.getOperatorConversation.mockResolvedValue({
      conversation: {
        id: "conversation-1",
        status: "active",
        activeRequestId: null,
      },
      frames: [
        {
          version: 1,
          conversationId: "conversation-1",
          requestId: "request-1",
          sequence: 1,
          type: "ui_command",
          payload: {
            effect: { id: "effect-1", kind: "theme", theme: "light" },
          },
          createdAt: 1,
        },
      ] satisfies OperatorFrame[],
    });

    await mount();

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(api.acknowledgeOperatorEffect).toHaveBeenCalledWith("conversation-1", "effect-1", {
      status: "rejected",
      clientRoute: "/",
      rejectionCode: "client_error",
    });
    expect(api.acknowledgeOperatorEffect).toHaveBeenCalledTimes(1);
  });
});
