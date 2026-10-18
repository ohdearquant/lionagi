import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { OperatorConversation, OperatorFrame, OperatorModelCatalogEntry } from "@/lib/types";

type StreamHandlers = {
  onFrame: (frame: OperatorFrame) => void;
  onConnection?: (state: "connecting" | "open" | "reconnecting") => void;
  onError?: (error: Error, fatal: boolean) => void;
};

const api = vi.hoisted(() => ({
  acknowledgeOperatorEffect: vi.fn(),
  cancelOperatorRequest: vi.fn(),
  createOperatorConversation: vi.fn(),
  decideOperatorProposal: vi.fn(),
  fetchOperatorModelCatalog: vi.fn(() =>
    Promise.resolve({ models: [] as OperatorModelCatalogEntry[] }),
  ),
  forkOperatorConversation: vi.fn(),
  getOperatorConversation: vi.fn(),
  listOperatorConversations: vi.fn(),
  reportOperatorView: vi.fn(() => Promise.resolve()),
  streamOperatorConversation: vi.fn(
    (_conversationId: string, _afterSequence: number, _handlers: StreamHandlers) => vi.fn(),
  ),
  submitOperatorTurn: vi.fn(),
  updateOperatorConversation: vi.fn(),
  getRunFile: vi.fn(),
}));
const router = vi.hoisted(() => ({ navigate: vi.fn() }));

vi.mock("@/lib/api", () => api);
vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, search }: { children?: React.ReactNode; search?: { s?: string } }) => (
    <a data-selected-session={search?.s} href="/fleet">
      {children}
    </a>
  ),
  useLocation: () => ({ pathname: "/", search: {} }),
  useSearch: () => ({}),
  useNavigate: () => router.navigate,
}));

const { default: OperatorPanel, formatProposalCommand } = await import("./OperatorPanel");

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
    api.reportOperatorView.mockReset();
    api.reportOperatorView.mockResolvedValue(undefined);
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

  it("links a run result using its session identity when CLI provenance is present", async () => {
    window.localStorage.setItem("studio:operator-conversation", "conversation-1");
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-1",
        title: "Run details",
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
        {
          version: 1,
          conversationId: "conversation-1",
          requestId: "request-1",
          sequence: 1,
          type: "tool_result",
          payload: {
            callId: "call-1",
            ok: true,
            result: {
              id: "session-1",
              sessionId: "session-1",
              runId: "20260829T120000-linked",
            },
          },
          createdAt: 1,
        },
      ],
    });

    await mount();

    expect(
      container.querySelector("[data-selected-session]")?.getAttribute("data-selected-session"),
    ).toBe("session-1");
  });

  it("links a session when its result has no CLI run provenance", async () => {
    window.localStorage.setItem("studio:operator-conversation", "conversation-1");
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-1",
        title: "Run details",
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
        {
          version: 1,
          conversationId: "conversation-1",
          requestId: "request-1",
          sequence: 1,
          type: "tool_result",
          payload: {
            callId: "call-1",
            ok: true,
            result: { id: "session-2", sessionId: "session-2", runId: null },
          },
          createdAt: 1,
        },
      ],
    });

    await mount();

    expect(
      container.querySelector("[data-selected-session]")?.getAttribute("data-selected-session"),
    ).toBe("session-2");
  });

  it("reopens a fatal stream after a turn is durably accepted", async () => {
    window.localStorage.setItem("studio:operator-conversation", "conversation-1");
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-1",
        title: "Restart recovery",
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
      frames: [textFrame(1, "assistant", "Before restart")],
    });
    api.submitOperatorTurn.mockResolvedValue({
      conversationId: "conversation-1",
      requestId: "request-after-restart",
      acceptedSequence: 2,
    });
    const subscriptions: StreamHandlers[] = [];
    const closers: ReturnType<typeof vi.fn>[] = [];
    api.streamOperatorConversation.mockImplementation(
      (_conversationId: string, _afterSequence: number, handlers: StreamHandlers) => {
        subscriptions.push(handlers);
        const close = vi.fn();
        closers.push(close);
        return close;
      },
    );
    const animationFrames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      animationFrames.push(callback);
      return animationFrames.length;
    });

    await mount();
    expect(subscriptions).toHaveLength(1);
    await act(async () => {
      subscriptions[0].onError?.(new Error("Connection lost — reload to continue."), true);
    });

    const textarea = container.querySelector("textarea") as HTMLTextAreaElement;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
      setter?.call(textarea, "sent during restart");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const send = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.startsWith("Send"),
    );
    await act(async () => {
      send?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(api.submitOperatorTurn).toHaveBeenCalledOnce();
    expect(closers[0]).toHaveBeenCalledOnce();
    expect(subscriptions).toHaveLength(2);
    await act(async () => {
      subscriptions[1].onConnection?.("connecting");
    });
    expect(container.textContent).toContain("Connection lost — reload to continue.");
    await act(async () => {
      subscriptions[1].onConnection?.("open");
      subscriptions[1].onFrame({
        ...textFrame(2, "user", "sent during restart"),
        requestId: "request-after-restart",
      });
      subscriptions[1].onFrame({
        ...textFrame(3, "assistant", ""),
        requestId: "request-after-restart",
        type: "done",
        payload: { outcome: "completed", lastSequence: 3 },
      });
      animationFrames.shift()?.(0);
    });

    expect(container.textContent).toContain("sent during restart");
    expect(container.textContent).not.toContain("Connection lost — reload to continue.");
    expect(
      [...container.querySelectorAll("button")].some((button) => button.textContent === "Stop"),
    ).toBe(false);
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
    const toggle = container.querySelector(
      'button[aria-label^="Conversations"]',
    ) as HTMLButtonElement;
    await act(async () => {
      toggle.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    const rows = container.querySelectorAll("ul li");
    expect(rows).toHaveLength(2);
    expect(container.textContent).toContain("Older daemon history");
  });

  it("announces the selected conversation on the trigger, before and after opening the list", async () => {
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-latest",
        title: "Latest daemon history",
        status: "active",
        activeRequestId: null,
        updatedAt: 20,
      },
    ]);
    api.getOperatorConversation.mockResolvedValue({
      conversation: {
        id: "conversation-latest",
        title: "Latest daemon history",
        status: "active",
        activeRequestId: null,
      },
      frames: [],
    });

    await mount();

    const toggle = container.querySelector(
      'button[aria-label^="Conversations"]',
    ) as HTMLButtonElement;
    expect(toggle.getAttribute("aria-label")).toBe("Conversations: Latest daemon history");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    await act(async () => {
      toggle.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(toggle.getAttribute("aria-label")).toBe("Conversations: Latest daemon history");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
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

    const toggle = container.querySelector(
      'button[aria-label^="Conversations"]',
    ) as HTMLButtonElement;
    await act(async () => {
      toggle.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    const priorRow = [...container.querySelectorAll("ul li button")].find((button) =>
      button.textContent?.includes("Prior conversation"),
    ) as HTMLButtonElement;
    await act(async () => {
      priorRow.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });

    expect(api.getOperatorConversation).toHaveBeenCalledWith("conversation-prior");
    expect(container.textContent).toContain("Prior daemon transcript");
    expect(window.localStorage.getItem("studio:operator-conversation")).toBe("conversation-prior");
  });

  function mockProposal(command: Record<string, unknown>, risk = "execute") {
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-1",
        title: "Permission review",
        status: "active",
        activeRequestId: null,
      },
    ]);
    api.getOperatorConversation.mockResolvedValue({
      conversation: { id: "conversation-1", status: "active", activeRequestId: null },
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
              command,
              commandHash: "sha256",
              risk,
              summary: "Update the changelog date.",
              idempotencyKey: "once",
              expiresAt: 999,
            },
          },
          createdAt: 1,
        },
      ] as unknown as OperatorFrame[],
    });
  }

  it("warns outside the disclosure when the rendered command is cut short", async () => {
    const command = {
      operation: "shell",
      note: "x".repeat(6_400),
      argv: ["rm", "-rf", "/etc"],
    };
    // No sensitive keys, no deep nesting, no long arrays: the redaction pass is the
    // identity here, so the dropped count is computable without the code under test.
    const dropped = JSON.stringify(command, null, 2).length - 6_000;
    expect(dropped).toBeGreaterThan(0);
    expect(dropped).toBeLessThan(1_000); // stays un-grouped in en number formatting
    mockProposal(command);

    await mount();

    const warning = container.querySelector('[data-testid="proposal-elided"]');
    expect(warning).not.toBeNull();
    // A warning inside <details> is one the operator can approve without ever seeing.
    expect(warning?.closest("details")).toBeNull();
    expect(warning?.textContent).toContain(String(dropped));
    expect(container.querySelector("pre")?.textContent).not.toContain("/etc");
  });

  it("does not warn about elision for a short flat command", async () => {
    mockProposal({ tool: "Bash", arguments: { command: "rm -rf /etc" } });

    await mount();

    expect(container.querySelector('[data-testid="proposal-elided"]')).toBeNull();
    expect(container.querySelector("pre")?.textContent).toContain("/etc");
  });

  it("opens the command disclosure by default so Allow is never live over a hidden command", async () => {
    mockProposal({ tool: "Bash", arguments: { command: "git status" } });

    await mount();

    const disclosure = container.querySelector("details");
    expect(disclosure).not.toBeNull();
    expect(disclosure?.open).toBe(true);
    expect(
      Array.from(container.querySelectorAll("button")).find(
        (button) => button.textContent === "Allow",
      ),
    ).toBeDefined();
  });

  describe("formatProposalCommand", () => {
    it("reports the length cut with the number of characters dropped", () => {
      const command = { note: "x".repeat(6_400) };
      const full = JSON.stringify(command, null, 2);
      const shown = formatProposalCommand(command);

      expect(shown.elided).toBe(true);
      expect(shown.droppedCharacters).toBe(full.length - 6_000);
      expect(shown.text.endsWith("\n…")).toBe(true);
    });

    it("reports the array cut", () => {
      const shown = formatProposalCommand({
        paths: [...Array.from({ length: 50 }, (_, index) => `/safe/${index}`), "/etc/shadow"],
      });

      expect(shown.elided).toBe(true);
      expect(shown.droppedCharacters).toBe(0);
      expect(shown.text).not.toContain("/etc/shadow");
      expect(shown.text).toContain("[1 more items]");
    });

    it("reports the depth cut", () => {
      let nested: Record<string, unknown> = { danger: "rm -rf /" };
      for (let depth = 0; depth < 8; depth += 1) nested = { nested };
      const shown = formatProposalCommand(nested);

      expect(shown.elided).toBe(true);
      expect(shown.text).not.toContain("rm -rf /");
      expect(shown.text).toContain("[truncated]");
    });

    it("reports redaction", () => {
      const shown = formatProposalCommand({ authorization: "Bearer secret" });

      expect(shown.elided).toBe(true);
      expect(shown.text).toContain("[redacted]");
    });

    it("reports a short flat command as complete", () => {
      const shown = formatProposalCommand({ argv: ["rm", "-rf", "/etc"] });

      expect(shown.elided).toBe(false);
      expect(shown.droppedCharacters).toBe(0);
      expect(shown.text).toContain("/etc");
    });
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

  describe("conversation list: rename, pin/archive, fork", () => {
    function deferred<T>() {
      let resolve!: (value: T) => void;
      const promise = new Promise<T>((complete) => {
        resolve = complete;
      });
      return { promise, resolve };
    }

    async function openList() {
      const toggle = container.querySelector(
        'button[aria-label^="Conversations"]',
      ) as HTMLButtonElement;
      await act(async () => {
        toggle.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });
    }

    beforeEach(() => {
      api.listOperatorConversations.mockResolvedValue([
        {
          id: "conversation-1",
          title: "Scheduler check",
          status: "active",
          pinned: false,
          activeRequestId: null,
          updatedAt: 2,
        },
      ]);
      api.getOperatorConversation.mockResolvedValue({
        conversation: {
          id: "conversation-1",
          title: "Scheduler check",
          status: "active",
          pinned: false,
          activeRequestId: null,
        },
        frames: [],
      });
    });

    it("shows a conversation description beneath its title and in the tooltip", async () => {
      const description = "Review scheduler retries and release decisions";
      const conversation = {
        id: "conversation-1",
        title: "Scheduler check",
        description,
        status: "active",
        pinned: false,
        activeRequestId: null,
      };
      api.listOperatorConversations.mockResolvedValue([conversation]);
      api.getOperatorConversation.mockResolvedValue({ conversation, frames: [] });

      await mount();
      await openList();

      const titleButton = [...container.querySelectorAll("ul li button")].find((button) =>
        button.textContent?.includes("Scheduler check"),
      ) as HTMLButtonElement;
      expect(titleButton.title).toBe(description);
      expect([...titleButton.querySelectorAll("span")].map((span) => span.textContent)).toEqual([
        "Scheduler check",
        description,
      ]);
    });

    describe("live conversation metadata refresh", () => {
      function metadataCall(sequence: number, requestId = "request-1"): OperatorFrame {
        return {
          ...textFrame(sequence, "assistant", ""),
          requestId,
          type: "tool_call",
          payload: {
            callId: "provider-call-1",
            tool: "mcp__studio_operator__update_conversation",
            arguments: { title: "Requested title" },
            mode: "draft",
          },
        };
      }

      function metadataResult(sequence: number, requestId = "request-1", ok = true): OperatorFrame {
        return {
          ...textFrame(sequence, "assistant", ""),
          requestId,
          type: "tool_result",
          payload: { callId: "provider-call-1", ok, result: { nativeToolCompleted: true } },
        };
      }

      function metadataDone(sequence: number, requestId = "request-1"): OperatorFrame {
        return {
          ...textFrame(sequence, "assistant", ""),
          requestId,
          type: "done",
          payload: { outcome: "failed", lastSequence: sequence },
        };
      }

      async function mountMetadataConversation(frames: OperatorFrame[] = []) {
        const conversation = {
          id: "conversation-1",
          title: "Scheduler check",
          description: "Original scope",
          status: "active" as const,
          pinned: true,
          activeRequestId: "request-1",
        };
        const other = {
          id: "conversation-2",
          title: "Other conversation",
          description: "Other scope",
          status: "active" as const,
          pinned: false,
          activeRequestId: null,
        };
        window.localStorage.setItem("studio:operator-conversation", conversation.id);
        api.listOperatorConversations.mockResolvedValue([conversation, other]);
        api.getOperatorConversation.mockResolvedValue({ conversation, frames });
        const animationFrames: FrameRequestCallback[] = [];
        vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
          animationFrames.push(callback);
          return animationFrames.length;
        });
        await mount();
        await openList();
        const handlers = api.streamOperatorConversation.mock.calls.at(-1)![2];
        const emit = async (...incoming: OperatorFrame[]) => {
          await act(async () => {
            for (const frame of incoming) handlers.onFrame(frame);
            animationFrames.shift()?.(0);
          });
        };
        return { conversation, other, emit };
      }

      function expectMetadata(title: string, description: string, pinned = true) {
        const picker = container.querySelector('button[aria-label^="Conversations"]');
        expect(picker?.getAttribute("aria-label")).toBe(`Conversations: ${title}`);
        const displayedTitle = pinned ? `★ ${title}` : title;
        const rowTitle = [...container.querySelectorAll("ul li button")].find(
          (button) => button.querySelector("span")?.textContent === displayedTitle,
        ) as HTMLButtonElement;
        expect(rowTitle).toBeDefined();
        expect(rowTitle.title).toBe(description);
        expect([...rowTitle.querySelectorAll("span")].map((span) => span.textContent)).toEqual([
          displayedTitle,
          description,
        ]);
        expect(
          rowTitle.closest("li")?.querySelector(`button[aria-label="${pinned ? "Unpin" : "Pin"}"]`),
        ).not.toBeNull();
        expect(api.acknowledgeOperatorEffect).not.toHaveBeenCalled();
      }

      it("rereads current metadata and the list after a paired successful native result", async () => {
        const { conversation, other, emit } = await mountMetadataConversation();
        const updated = { ...conversation, title: "Stored title", description: "Stored scope" };
        api.getOperatorConversation.mockResolvedValue({ conversation: updated, frames: [] });
        api.listOperatorConversations.mockResolvedValue([updated, other]);

        await emit(metadataCall(1));
        expect(api.getOperatorConversation).toHaveBeenCalledOnce();
        await emit(metadataResult(2));

        expectMetadata("Stored title", "Stored scope");
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(2);
        expect(api.listOperatorConversations).toHaveBeenLastCalledWith({ status: "active" });
        expect(api.listOperatorConversations).toHaveBeenCalledTimes(2);
        expect(api.streamOperatorConversation).toHaveBeenCalledOnce();
        await emit(metadataResult(2), metadataDone(3));
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(2);
      });

      it("rereads on done when the provider never reports the metadata call result", async () => {
        const { conversation, other, emit } = await mountMetadataConversation();
        const updated = { ...conversation, title: "Durable title", description: "Durable scope" };
        api.getOperatorConversation.mockResolvedValue({ conversation: updated, frames: [] });
        api.listOperatorConversations.mockResolvedValue([updated, other]);

        await emit(metadataCall(1), metadataDone(2));

        expectMetadata("Durable title", "Durable scope");
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(2);
        expect(api.listOperatorConversations).toHaveBeenCalledTimes(2);
      });

      it("pairs call ids within the same request and conversation", async () => {
        const { conversation, other, emit } = await mountMetadataConversation([metadataCall(1)]);
        const updated = { ...conversation, title: "Paired title", description: "Paired scope" };
        api.getOperatorConversation.mockResolvedValue({ conversation: updated, frames: [] });
        api.listOperatorConversations.mockResolvedValue([updated, other]);

        await emit({
          ...metadataResult(2),
          payload: { callId: "different-call", ok: true, result: { nativeToolCompleted: true } },
        });
        await emit(metadataResult(3, "another-request"));
        await emit({ ...metadataResult(4), conversationId: "conversation-2" });
        expect(api.getOperatorConversation).toHaveBeenCalledOnce();
        await emit(metadataResult(5));

        expectMetadata("Paired title", "Paired scope");
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(2);
        await emit(
          {
            ...metadataCall(6, "request-2"),
            payload: { callId: "provider-call-1", tool: "other_tool", arguments: {}, mode: "read" },
          },
          metadataResult(7, "request-2"),
          metadataDone(8, "request-2"),
        );
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(2);
      });

      it("does not refresh a failed result again at done", async () => {
        const { conversation, other, emit } = await mountMetadataConversation();
        const updated = {
          ...conversation,
          title: "Confirmed title",
          description: "Confirmed scope",
        };
        api.getOperatorConversation.mockResolvedValue({ conversation: updated, frames: [] });
        api.listOperatorConversations.mockResolvedValue([updated, other]);
        await emit(metadataCall(1), metadataResult(2));
        expectMetadata("Confirmed title", "Confirmed scope");

        await emit(metadataCall(3, "request-2"), metadataResult(4, "request-2", false));
        await emit(metadataDone(5, "request-2"));
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(2);
        expect(api.listOperatorConversations).toHaveBeenCalledTimes(2);
      });

      it("ignores an older metadata read while a newer refresh is pending", async () => {
        const { conversation, other, emit } = await mountMetadataConversation();
        const older = { ...conversation, title: "Older title", description: "Older scope" };
        const newer = { ...conversation, title: "Newer title", description: "Newer scope" };
        const first = deferred<{ conversation: typeof conversation; frames: OperatorFrame[] }>();
        const second = deferred<{ conversation: typeof conversation; frames: OperatorFrame[] }>();
        api.getOperatorConversation
          .mockReturnValueOnce(first.promise)
          .mockReturnValueOnce(second.promise);
        api.listOperatorConversations
          .mockResolvedValueOnce([older, other])
          .mockResolvedValueOnce([newer, other]);

        await emit(metadataCall(1), metadataResult(2));
        await emit(metadataCall(3, "request-2"), metadataResult(4, "request-2"));
        await act(async () => {
          first.resolve({ conversation: older, frames: [] });
        });
        expectMetadata("Scheduler check", "Original scope");
        await act(async () => {
          second.resolve({ conversation: newer, frames: [] });
        });
        expectMetadata("Newer title", "Newer scope");
      });

      it("rereads after a delayed pin response invalidates the metadata snapshot", async () => {
        const { conversation, other, emit } = await mountMetadataConversation();
        const unpinned = { ...conversation, pinned: false };
        const updated = { ...unpinned, title: "Stored title", description: "Stored scope" };
        const pin = deferred<typeof conversation>();
        const first = deferred<{ conversation: typeof conversation; frames: OperatorFrame[] }>();
        const second = deferred<{ conversation: typeof conversation; frames: OperatorFrame[] }>();
        api.updateOperatorConversation.mockReturnValueOnce(pin.promise);
        const unpinButton = container.querySelector(
          'button[aria-label="Unpin"]',
        ) as HTMLButtonElement;
        await act(async () => {
          unpinButton.click();
        });
        api.getOperatorConversation
          .mockReturnValueOnce(first.promise)
          .mockReturnValueOnce(second.promise);
        api.listOperatorConversations.mockResolvedValue([updated, other]);

        await emit(metadataCall(1), metadataResult(2));
        await act(async () => {
          pin.resolve(unpinned);
        });
        expectMetadata("Scheduler check", "Original scope");
        await act(async () => {
          first.resolve({ conversation: updated, frames: [] });
        });
        expectMetadata("Scheduler check", "Original scope");
        await act(async () => {
          second.resolve({ conversation: updated, frames: [] });
        });

        expectMetadata("Stored title", "Stored scope", false);
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(3);
        expect(api.listOperatorConversations).toHaveBeenCalledTimes(3);
        await emit(metadataDone(3));
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(3);
      });

      it("rereads when a delayed pin response arrives after the metadata refresh", async () => {
        const { conversation, other, emit } = await mountMetadataConversation();
        const unpinned = { ...conversation, pinned: false };
        const updated = { ...unpinned, title: "Stored title", description: "Stored scope" };
        const current = { ...updated, title: "Current title", description: "Current scope" };
        const pin = deferred<typeof conversation>();
        const second = deferred<{ conversation: typeof conversation; frames: OperatorFrame[] }>();
        api.updateOperatorConversation.mockReturnValueOnce(pin.promise);
        const unpinButton = container.querySelector(
          'button[aria-label="Unpin"]',
        ) as HTMLButtonElement;
        await act(async () => {
          unpinButton.click();
        });
        api.getOperatorConversation
          .mockResolvedValueOnce({ conversation: updated, frames: [] })
          .mockReturnValueOnce(second.promise);
        api.listOperatorConversations
          .mockResolvedValueOnce([updated, other])
          .mockResolvedValueOnce([current, other]);

        await emit(metadataCall(1), metadataResult(2));
        expectMetadata("Stored title", "Stored scope", false);
        await act(async () => {
          pin.resolve(unpinned);
        });
        expectMetadata("Stored title", "Stored scope", false);
        await act(async () => {
          second.resolve({ conversation: current, frames: [] });
        });

        expectMetadata("Current title", "Current scope", false);
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(3);
        expect(api.listOperatorConversations).toHaveBeenCalledTimes(3);
        await emit(metadataDone(3));
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(3);
      });

      it("preserves a selected metadata refresh when a background row is saved", async () => {
        const { conversation, other, emit } = await mountMetadataConversation();
        const updated = { ...conversation, title: "Stored title", description: "Stored scope" };
        const pinnedOther = { ...other, pinned: true };
        const first = deferred<{ conversation: typeof conversation; frames: OperatorFrame[] }>();
        const pin = deferred<typeof other>();
        api.getOperatorConversation
          .mockReturnValueOnce(first.promise)
          .mockResolvedValue({ conversation: updated, frames: [] });
        api.listOperatorConversations.mockResolvedValue([updated, other]);
        await emit(metadataCall(1), metadataResult(2));
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(2);

        api.updateOperatorConversation.mockReturnValueOnce(pin.promise);
        const otherTitle = [...container.querySelectorAll("ul li button")].find(
          (button) => button.querySelector("span")?.textContent === "Other conversation",
        ) as HTMLButtonElement;
        const pinButton = otherTitle
          .closest("li")
          ?.querySelector('button[aria-label="Pin"]') as HTMLButtonElement;
        await act(async () => pinButton.click());
        expect(api.updateOperatorConversation).toHaveBeenLastCalledWith("conversation-2", {
          pinned: true,
        });
        api.listOperatorConversations.mockResolvedValue([updated, pinnedOther]);
        await act(async () => pin.resolve(pinnedOther));
        await act(async () => first.resolve({ conversation: updated, frames: [] }));

        expectMetadata("Stored title", "Stored scope");
        const savedOther = [...container.querySelectorAll("ul li button")].find(
          (button) => button.querySelector("span")?.textContent === "★ Other conversation",
        ) as HTMLButtonElement;
        expect(savedOther).toBeDefined();
        expect(savedOther.title).toBe("Other scope");
        expect(
          savedOther.closest("li")?.querySelector('button[aria-label="Unpin"]'),
        ).not.toBeNull();
        await emit(metadataDone(3));
        expectMetadata("Stored title", "Stored scope");
      });

      it("discards an in-flight metadata read after selecting another conversation", async () => {
        const { conversation, other, emit } = await mountMetadataConversation();
        const updated = { ...conversation, title: "Delayed title", description: "Delayed scope" };
        const pending = deferred<{ conversation: typeof conversation; frames: OperatorFrame[] }>();
        api.getOperatorConversation.mockReturnValueOnce(pending.promise);
        api.listOperatorConversations.mockResolvedValueOnce([updated, other]);
        await emit(metadataCall(1), metadataResult(2));
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(2);
        api.getOperatorConversation.mockResolvedValueOnce({ conversation: other, frames: [] });
        const otherButton = [...container.querySelectorAll("ul li button")].find(
          (button) => button.querySelector("span")?.textContent === "Other conversation",
        ) as HTMLButtonElement;
        await act(async () => {
          otherButton.click();
        });
        await act(async () => {
          pending.resolve({ conversation: updated, frames: [] });
        });
        await openList();

        expect(
          container
            .querySelector('button[aria-label^="Conversations"]')
            ?.getAttribute("aria-label"),
        ).toBe("Conversations: Other conversation");
        expect(container.querySelector("ul")?.textContent).toContain("Scheduler check");
        expect(container.querySelector("ul")?.textContent).not.toContain("Delayed title");
      });
    });

    describe("local conversation response ordering", () => {
      beforeEach(() => {
        api.updateOperatorConversation.mockReset();
      });

      async function mountLocalConversation(selectedId = "conversation-1") {
        const conversation: OperatorConversation = {
          id: "conversation-1",
          title: "Scheduler check",
          description: "Original scope",
          status: "active",
          pinned: true,
          activeRequestId: null,
        };
        const other: OperatorConversation = {
          id: "conversation-2",
          title: "Other conversation",
          description: "Other scope",
          status: "active",
          pinned: false,
          activeRequestId: null,
        };
        const stored = new Map([conversation, other].map((row) => [row.id, row]));
        const rows = () => [...stored.values()];
        api.listOperatorConversations.mockImplementation(async (options?: { status?: string }) =>
          rows().filter((row) => !options?.status || row.status === options.status),
        );
        api.getOperatorConversation.mockImplementation(async (id: string) => ({
          conversation: stored.get(id),
          frames: [],
        }));
        window.localStorage.setItem("studio:operator-conversation", selectedId);
        await mount();
        await openList();
        return {
          conversation,
          other,
          rows,
          store: (row: OperatorConversation) => stored.set(row.id, row),
        };
      }

      function titleButton(title: string) {
        const button = [...container.querySelectorAll("ul li button")].find((candidate) => {
          const label = candidate.querySelector("span")?.textContent;
          return label === title || label === `★ ${title}`;
        }) as HTMLButtonElement | undefined;
        expect(button).toBeDefined();
        return button!;
      }

      function rowAction(title: string, label: string) {
        const button = titleButton(title)
          .closest("li")
          ?.querySelector(`button[aria-label="${label}"]`) as HTMLButtonElement | null;
        expect(button).not.toBeNull();
        return button!;
      }

      function expectStoredRow(
        conversation: OperatorConversation,
        selectedTitle = conversation.title,
      ) {
        const title = titleButton(conversation.title!);
        expect(title.querySelector("span")?.textContent).toBe(
          `${conversation.pinned ? "★ " : ""}${conversation.title}`,
        );
        expect(title.title).toBe(conversation.description);
        expect(title.querySelectorAll("span")[1]?.textContent).toBe(conversation.description);
        expect(rowAction(conversation.title!, conversation.pinned ? "Unpin" : "Pin")).toBeDefined();
        expect(
          container
            .querySelector('button[aria-label^="Conversations"]')
            ?.getAttribute("aria-label"),
        ).toBe(`Conversations: ${selectedTitle}`);
        expect(api.acknowledgeOperatorEffect).not.toHaveBeenCalled();
      }

      async function enterRename(title: string, draft: string) {
        await act(async () => {
          titleButton(title).dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
        });
        const input = container.querySelector("ul li input") as HTMLInputElement;
        expect(input).not.toBeNull();
        const setNativeValue = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype,
          "value",
        )!.set!;
        await act(async () => {
          setNativeValue.call(input, draft);
          input.dispatchEvent(new Event("input", { bubbles: true }));
        });
        return input;
      }

      async function selectFilter(label: "Active" | "Archived") {
        const button = [...container.querySelectorAll("button")].find(
          (candidate) => candidate.textContent === label,
        ) as HTMLButtonElement;
        expect(button).toBeDefined();
        await act(async () => button.click());
      }

      it("serializes blur rename and unpin before a rerender", async () => {
        const { conversation, store } = await mountLocalConversation();
        const renamed = { ...conversation, title: "Renamed check" };
        const unpinned = { ...renamed, pinned: false };
        const rename = deferred<OperatorConversation>();
        const unpin = deferred<OperatorConversation>();
        api.updateOperatorConversation
          .mockReturnValueOnce(rename.promise)
          .mockReturnValueOnce(unpin.promise);
        const unpinButton = rowAction("Scheduler check", "Unpin");
        const input = await enterRename("Scheduler check", "Renamed check");

        await act(async () => {
          input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
          unpinButton.click();
        });

        expect(api.updateOperatorConversation).toHaveBeenCalledExactlyOnceWith("conversation-1", {
          title: "Renamed check",
        });
        expect(rowAction("Scheduler check", "Unpin").disabled).toBe(true);
        await act(async () => {
          titleButton("Scheduler check").dispatchEvent(
            new MouseEvent("dblclick", { bubbles: true }),
          );
        });
        expect(container.querySelector("ul li input")).toBeNull();

        store(renamed);
        await act(async () => rename.resolve(renamed));
        expectStoredRow(renamed);
        await act(async () => rowAction("Renamed check", "Unpin").click());
        expect(api.updateOperatorConversation).toHaveBeenLastCalledWith("conversation-1", {
          pinned: false,
        });
        store(unpinned);
        await act(async () => unpin.resolve(unpinned));
        expectStoredRow(unpinned);
      });

      it("does not enter rename while an unpin save is pending", async () => {
        const { conversation, store } = await mountLocalConversation();
        const unpinned = { ...conversation, pinned: false };
        const unpin = deferred<OperatorConversation>();
        api.updateOperatorConversation.mockReturnValueOnce(unpin.promise);
        await act(async () => rowAction("Scheduler check", "Unpin").click());
        await act(async () => {
          titleButton("Scheduler check").dispatchEvent(
            new MouseEvent("dblclick", { bubbles: true }),
          );
        });
        expect(container.querySelector("ul li input")).toBeNull();
        expect(api.updateOperatorConversation).toHaveBeenCalledExactlyOnceWith("conversation-1", {
          pinned: false,
        });

        store(unpinned);
        await act(async () => unpin.resolve(unpinned));
        expectStoredRow(unpinned);
        const renamed = { ...unpinned, title: "Renamed check" };
        api.updateOperatorConversation.mockResolvedValueOnce(renamed);
        const input = await enterRename("Scheduler check", "Renamed check");
        store(renamed);
        await act(async () => {
          input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
        });
        expectStoredRow(renamed);
      });

      it.each([
        ["list-first", "conversation-1"],
        ["patch-first", "conversation-1"],
        ["list-first", "conversation-2"],
        ["patch-first", "conversation-2"],
      ])(
        "keeps an unpin after an older list response arrives (%s, selected %s)",
        async (order, selectedId) => {
          const { conversation, other, rows, store } = await mountLocalConversation(selectedId);
          const unpinned = { ...conversation, pinned: false };
          const oldList = deferred<void>();
          const unpin = deferred<OperatorConversation>();
          api.listOperatorConversations.mockImplementationOnce(() => {
            // Capture at request time: releasing the response later must not turn
            // an old server snapshot into a fresh one by reading the map again.
            const snapshot = rows();
            return oldList.promise.then(() => snapshot);
          });
          api.updateOperatorConversation.mockReturnValueOnce(unpin.promise);
          if (order === "list-first") await selectFilter("Active");
          await act(async () => rowAction("Scheduler check", "Unpin").click());
          if (order === "patch-first") await selectFilter("Active");
          expect(api.updateOperatorConversation).toHaveBeenCalledExactlyOnceWith("conversation-1", {
            pinned: false,
          });

          store(unpinned);
          await act(async () => unpin.resolve(unpinned));
          await act(async () => oldList.resolve());

          expectStoredRow(
            unpinned,
            selectedId === conversation.id ? conversation.title : other.title,
          );
          expect(rowAction("Scheduler check", "Pin").disabled).toBe(false);
        },
      );

      it("keeps the latest filter when an earlier list read resolves", async () => {
        const { conversation, other } = await mountLocalConversation();
        const archived = { ...other, status: "archived" as const };
        const activeList = deferred<OperatorConversation[]>();
        api.listOperatorConversations
          .mockReturnValueOnce(activeList.promise)
          .mockResolvedValueOnce([archived]);
        await selectFilter("Active");
        await selectFilter("Archived");
        expectStoredRow(archived, conversation.title);
        await act(async () => activeList.resolve([conversation, other]));

        expectStoredRow(archived, conversation.title);
        expect(container.querySelector("ul")?.textContent).not.toContain("Scheduler check");
        expect(api.listOperatorConversations).toHaveBeenLastCalledWith({ status: "archived" });
      });

      it("keeps a saved row when another conversation is selected before the save settles", async () => {
        const { conversation, other, rows, store } = await mountLocalConversation("conversation-1");
        const unpinned = { ...conversation, pinned: false };
        const save = deferred<OperatorConversation>();
        const selectedRead = deferred<{
          conversation: typeof conversation;
          frames: OperatorFrame[];
        }>();
        const refreshList = deferred<void>();
        api.updateOperatorConversation.mockReturnValueOnce(save.promise);
        api.getOperatorConversation.mockReturnValueOnce(selectedRead.promise);
        await act(async () => rowAction("Scheduler check", "Unpin").click());

        await act(async () => titleButton("Other conversation").click());
        api.listOperatorConversations.mockImplementationOnce(() =>
          refreshList.promise.then(() => rows()),
        );
        store(unpinned);
        await act(async () => save.resolve(unpinned));
        await act(async () => {
          selectedRead.resolve({ conversation: other, frames: [] });
        });
        await act(async () => refreshList.resolve());

        await openList();
        expectStoredRow(unpinned, other.title);
      });

      it("loads a conversation when the first list read is superseded by a filter", async () => {
        const conversation: OperatorConversation = {
          id: "conversation-1",
          title: "Scheduler check",
          status: "active",
          pinned: false,
          activeRequestId: null,
        };
        const initialList = deferred<OperatorConversation[]>();
        const filteredList = deferred<OperatorConversation[]>();
        api.listOperatorConversations
          .mockReturnValueOnce(initialList.promise)
          .mockReturnValueOnce(filteredList.promise);
        api.getOperatorConversation.mockResolvedValue({ conversation, frames: [] });

        await mount();
        await openList();
        await selectFilter("Active");
        await act(async () => filteredList.resolve([conversation]));
        await act(async () => initialList.resolve([conversation]));

        expect(api.getOperatorConversation).toHaveBeenCalledOnce();
        expect(
          container
            .querySelector('button[aria-label^="Conversations"]')
            ?.getAttribute("aria-label"),
        ).toBe("Conversations: Scheduler check");
      });

      it("refreshes listed rows after a fork supersedes a metadata read", async () => {
        const { conversation, other, rows, store } = await mountLocalConversation();
        const renamed = { ...conversation, title: "Renamed check" };
        const pendingRead = deferred<{
          conversation: typeof conversation;
          frames: OperatorFrame[];
        }>();
        api.getOperatorConversation.mockReturnValueOnce(pendingRead.promise);
        api.listOperatorConversations.mockResolvedValueOnce([conversation, other]);

        await selectFilter("Active");
        api.forkOperatorConversation.mockResolvedValue({
          conversation: {
            id: "conversation-fork",
            title: "Scheduler check (fork)",
            status: "active",
            pinned: false,
            activeRequestId: null,
          },
          frames: [],
        });
        store(renamed);
        api.listOperatorConversations.mockImplementationOnce(() => Promise.resolve(rows()));
        await act(async () => rowAction("Scheduler check", "Fork").click());
        await act(async () => {
          pendingRead.resolve({ conversation, frames: [] });
        });

        await openList();
        expect(container.querySelector("ul")?.textContent).toContain("Renamed check");
      });
    });

    it("renames a conversation inline and reflects the new title", async () => {
      window.localStorage.setItem("studio:operator-conversation", "conversation-1");
      api.updateOperatorConversation.mockResolvedValue({
        id: "conversation-1",
        title: "Renamed check",
        status: "active",
        pinned: false,
        activeRequestId: null,
      });

      await mount();
      await openList();

      const titleButton = [...container.querySelectorAll("ul li button")].find((button) =>
        button.textContent?.includes("Scheduler check"),
      ) as HTMLButtonElement;
      await act(async () => {
        titleButton.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
      });
      const input = container.querySelector("ul li input") as HTMLInputElement;
      const setNativeValue = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        "value",
      )!.set!;
      await act(async () => {
        setNativeValue.call(input, "Renamed check");
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
        await Promise.resolve();
      });

      expect(api.updateOperatorConversation).toHaveBeenCalledWith("conversation-1", {
        title: "Renamed check",
      });
      expect(container.textContent).toContain("Renamed check");
    });

    it("keeps focus somewhere reachable when the row being renamed is archived away", async () => {
      window.localStorage.setItem("studio:operator-conversation", "conversation-1");
      // Archiving the row under an active-only filter removes it while its
      // rename input still holds focus. Removing a focused element moves focus
      // to the body and fires no blur, so nothing hands it back on its own.
      api.updateOperatorConversation.mockResolvedValue({
        id: "conversation-1",
        title: "Scheduler check",
        status: "archived",
        pinned: false,
        activeRequestId: null,
      });

      await mount();
      await openList();

      const titleButton = [...container.querySelectorAll("ul li button")].find((button) =>
        button.textContent?.includes("Scheduler check"),
      ) as HTMLButtonElement;
      await act(async () => {
        titleButton.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
      });
      expect(container.querySelector("ul li input")).not.toBeNull();

      const archiveButton = [...container.querySelectorAll("ul li button")].find(
        (button) => button.getAttribute("aria-label") === "Archive",
      ) as HTMLButtonElement;
      await act(async () => {
        archiveButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(container.querySelector("ul li input")).toBeNull();
      expect(document.activeElement).not.toBe(document.body);
      expect(document.activeElement?.getAttribute("aria-label")).toMatch(/^Conversations/);
    });

    it("surfaces an error when renaming a conversation that no longer exists", async () => {
      window.localStorage.setItem("studio:operator-conversation", "conversation-1");
      api.updateOperatorConversation.mockRejectedValue(
        new Error("Operator conversation 'conversation-1' not found"),
      );

      await mount();
      await openList();

      const titleButton = [...container.querySelectorAll("ul li button")].find((button) =>
        button.textContent?.includes("Scheduler check"),
      ) as HTMLButtonElement;
      await act(async () => {
        titleButton.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
      });
      const input = container.querySelector("ul li input") as HTMLInputElement;
      const setNativeValue = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        "value",
      )!.set!;
      await act(async () => {
        setNativeValue.call(input, "New title");
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
        await Promise.resolve();
      });

      expect(container.textContent).toContain("not found");
    });

    it("pins a conversation to the top and archives it out of the active list", async () => {
      window.localStorage.setItem("studio:operator-conversation", "conversation-1");
      api.updateOperatorConversation.mockImplementation(
        (_id: string, patch: Record<string, unknown>) =>
          Promise.resolve({
            id: "conversation-1",
            title: "Scheduler check",
            status: patch.status ?? "active",
            pinned: patch.pinned ?? false,
            activeRequestId: null,
          }),
      );

      await mount();
      await openList();

      const pinButton = container.querySelector(
        'ul li button[aria-label="Pin"]',
      ) as HTMLButtonElement;
      await act(async () => {
        pinButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        await Promise.resolve();
      });
      expect(api.updateOperatorConversation).toHaveBeenCalledWith("conversation-1", {
        pinned: true,
      });

      const archiveButton = container.querySelector(
        'ul li button[aria-label="Archive"]',
      ) as HTMLButtonElement;
      await act(async () => {
        archiveButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        await Promise.resolve();
      });
      expect(api.updateOperatorConversation).toHaveBeenCalledWith("conversation-1", {
        status: "archived",
      });
      // The active filter is still selected, so an archived conversation drops out of the
      // list rows (the header keeps showing the still-open conversation's own title).
      expect(container.querySelector("ul")?.textContent).toContain("No conversations yet");
      expect(container.querySelector("ul")?.textContent).not.toContain("Scheduler check");
    });

    it("forks a conversation and switches to the new one", async () => {
      window.localStorage.setItem("studio:operator-conversation", "conversation-1");
      api.forkOperatorConversation.mockResolvedValue({
        conversation: {
          id: "conversation-fork",
          title: "Scheduler check (fork)",
          status: "active",
          pinned: false,
          activeRequestId: null,
        },
        frames: [
          { ...textFrame(1, "assistant", "Forked history"), conversationId: "conversation-fork" },
        ],
      });

      await mount();
      await openList();

      const forkButton = container.querySelector(
        'ul li button[aria-label="Fork"]',
      ) as HTMLButtonElement;
      await act(async () => {
        forkButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        await Promise.resolve();
      });

      expect(api.forkOperatorConversation).toHaveBeenCalledWith("conversation-1");
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe("conversation-fork");
      expect(container.textContent).toContain("Forked history");
    });
  });

  describe("the model menu and a conversation's stored pin", () => {
    const pinned = {
      id: "conversation-1",
      title: "Pinned",
      status: "active" as const,
      activeRequestId: null,
      provider: "codex",
      providerModel: "gpt-5.4",
    };

    function pin(conversation: Record<string, unknown>) {
      api.listOperatorConversations.mockResolvedValue([conversation]);
      api.getOperatorConversation.mockResolvedValue({ conversation, frames: [] });
      api.submitOperatorTurn.mockReset();
      api.submitOperatorTurn.mockResolvedValue({
        conversationId: conversation.id,
        requestId: "request-1",
        acceptedSequence: 1,
      });
    }

    function modelSelect() {
      return container.querySelector('select[aria-label="Model"]') as HTMLSelectElement;
    }

    async function send(text: string) {
      const textarea = container.querySelector("textarea") as HTMLTextAreaElement;
      await act(async () => {
        const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
        setter?.call(textarea, text);
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
      });
      const sendButton = [...container.querySelectorAll("button")].find((button) =>
        button.textContent?.startsWith("Send"),
      );
      expect(sendButton, "the composer's Send button").toBeDefined();
      expect(sendButton?.disabled).toBe(false);
      await act(async () => {
        sendButton?.click();
        await Promise.resolve();
        await Promise.resolve();
      });
    }

    it("keeps the model and effort selects out of the header, beside Send", async () => {
      pin(pinned);
      api.fetchOperatorModelCatalog.mockResolvedValue({
        models: [
          { id: "gpt-5.4", label: "Codex (gpt-5.4)", provider: "codex", efforts: ["high", "low"] },
        ],
      });

      await mount();

      // Both selects are native, so each takes its intrinsic width from its
      // widest option. In the header they also carried shrink-0, so instead of
      // yielding to the conversation picker beside them they overran it and the
      // two painted over each other. Asserting on ancestry rather than on class
      // names means this fails for the layout being wrong, not for the styling
      // being rewritten.
      const header = container.querySelector("header");
      const composer = container.querySelector("footer");
      expect(header, "the panel header").not.toBeNull();
      expect(composer, "the composer footer").not.toBeNull();

      const model = modelSelect();
      const effort = container.querySelector(
        'select[aria-label="Effort"]',
      ) as HTMLSelectElement | null;
      expect(model, "the model select").not.toBeNull();
      expect(effort, "the effort select").not.toBeNull();

      expect(header!.contains(model)).toBe(false);
      expect(composer!.contains(model)).toBe(true);
      expect(header!.contains(effort!)).toBe(false);
      expect(composer!.contains(effort!)).toBe(true);

      // Beside Send specifically, not merely somewhere in the footer: the two
      // share a parent, which is what keeps them reachable in one glance.
      const send = [...composer!.querySelectorAll("button")].find((button) =>
        button.textContent?.startsWith("Send"),
      );
      expect(send, "the Send button").toBeDefined();
      expect(model.parentElement?.parentElement).toBe(send!.parentElement);

      // The header still has to carry the conversation picker; a fix that
      // emptied the row rather than moving two controls out of it would
      // otherwise pass every assertion above.
      expect(header!.querySelector("button[aria-expanded]")).not.toBeNull();
    });

    it("shows the pinned model rather than reporting Default", async () => {
      pin(pinned);
      api.fetchOperatorModelCatalog.mockResolvedValue({
        models: [
          { id: "gpt-5.4", label: "Codex (gpt-5.4)", provider: "codex", efforts: ["high"] },
          { id: "sonnet", label: "Claude Sonnet", provider: "claude_code", efforts: ["high"] },
        ],
      });

      await mount();

      // The daemon keeps using this pin for a turn that names no model, so a
      // menu reading "Default" would state the opposite of what will run.
      expect(modelSelect().value).toBe("gpt-5.4");
    });

    it("groups recommended models by provider and explains the selected effort contract", async () => {
      pin(pinned);
      api.fetchOperatorModelCatalog.mockResolvedValue({
        // Deliberately interleaved: the picker, not backend ordering, owns the
        // provider groups shown to the operator.
        models: [
          {
            id: "gpt-5.4",
            label: "Codex (gpt-5.4)",
            provider: "codex",
            efforts: ["low", "high", "ultra"],
          },
          {
            id: "sonnet",
            label: "Claude Sonnet",
            provider: "claude_code",
            efforts: ["low", "medium", "high"],
          },
          {
            id: "gemini-3.6-flash",
            label: "Gemini 3.6 Flash",
            provider: "gemini_code",
            efforts: ["low", "medium", "high"],
          },
          {
            id: "gpt-5.5",
            label: "Codex (gpt-5.5)",
            provider: "codex",
            efforts: ["medium", "high"],
          },
        ],
      });

      await mount();

      const select = modelSelect();
      const groups = [...select.querySelectorAll("optgroup")];
      expect(groups.map((group) => group.label)).toEqual([
        "Claude · recommended",
        "Codex · recommended",
        "Gemini · recommended",
      ]);
      expect([...groups[0].querySelectorAll("option")].map((option) => option.value)).toEqual([
        "sonnet",
      ]);
      expect([...groups[1].querySelectorAll("option")].map((option) => option.value)).toEqual([
        "gpt-5.4",
        "gpt-5.5",
      ]);
      expect([...groups[2].querySelectorAll("option")].map((option) => option.value)).toEqual([
        "gemini-3.6-flash",
      ]);

      // The menu exposes the ceiling before selection, rather than making the
      // operator discover supported effort only after changing models.
      expect(groups[2].textContent).toContain("low / medium / high");
      expect(
        container.querySelector('[data-testid="operator-model-consequence"]')?.textContent,
      ).toContain("Effort is sent as a provider setting");

      await act(async () => {
        const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
        setter?.call(select, "gemini-3.6-flash");
        select.dispatchEvent(new Event("change", { bubbles: true }));
      });

      const consequence = container.querySelector('[data-testid="operator-model-consequence"]');
      expect(consequence?.textContent).toContain("Gemini");
      expect(consequence?.textContent).toContain("Effort: low / medium / high");
      expect(consequence?.textContent).toContain("Effort is encoded in the model ID");
      expect(select.getAttribute("aria-describedby")).toBe(consequence?.id);
    });

    it("names a pinned model the catalog no longer offers instead of hiding it", async () => {
      pin({ ...pinned, providerModel: "gpt-5.3-retired" });
      api.fetchOperatorModelCatalog.mockResolvedValue({
        models: [{ id: "sonnet", label: "Claude Sonnet", provider: "claude_code", efforts: [] }],
      });

      await mount();

      const select = modelSelect();
      expect(select.value).toBe("gpt-5.3-retired");
      const option = [...select.options].find((item) => item.value === "gpt-5.3-retired");
      expect(option?.textContent).toContain("unavailable");
      const groups = [...select.querySelectorAll("optgroup")];
      expect(groups.at(-1)?.label).toBe("Legacy selection");
      expect(groups.at(-1)?.querySelector("option")?.value).toBe("gpt-5.3-retired");
      expect(groups.slice(0, -1).every((group) => !group.contains(option!))).toBe(true);
    });

    it("asks for the pin to be dropped when the menu is moved back to Default", async () => {
      pin(pinned);
      api.fetchOperatorModelCatalog.mockResolvedValue({
        models: [{ id: "gpt-5.4", label: "Codex (gpt-5.4)", provider: "codex", efforts: [] }],
      });

      await mount();
      const select = modelSelect();
      await act(async () => {
        const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
        setter?.call(select, "");
        select.dispatchEvent(new Event("change", { bubbles: true }));
      });
      await send("go");

      // Omitting the model would leave the pin in force. Selecting Default has
      // to be able to undo a pin, so it says so explicitly.
      expect(api.submitOperatorTurn).toHaveBeenCalledTimes(1);
      const [, request] = api.submitOperatorTurn.mock.calls[0];
      expect(request.clearSelection).toBe(true);
      expect(request.model).toBeUndefined();
    });

    it("does not ask to clear a conversation that was never pinned", async () => {
      pin({ id: "conversation-1", title: "Fresh", status: "active", activeRequestId: null });
      api.fetchOperatorModelCatalog.mockResolvedValue({
        models: [{ id: "gpt-5.4", label: "Codex (gpt-5.4)", provider: "codex", efforts: [] }],
      });

      await mount();
      expect(modelSelect().value).toBe("");
      await send("go");

      expect(api.submitOperatorTurn).toHaveBeenCalledTimes(1);
      const [, request] = api.submitOperatorTurn.mock.calls[0];
      expect(request.clearSelection).toBeUndefined();
    });
  });
});
