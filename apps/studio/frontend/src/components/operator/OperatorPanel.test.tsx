import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import { ApiError } from "@/lib/api";
import type {
  OperatorConversation,
  OperatorConversationSnapshot,
  OperatorFrame,
  OperatorModelCatalogEntry,
  OperatorTruncationPayload,
} from "@/lib/types";

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

vi.mock("@/lib/api", async (importOriginal) => {
  const { ApiError } = await importOriginal<typeof import("@/lib/api")>();
  return { ...api, ApiError };
});
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

function truncationFrame(sequence: number): OperatorFrame<OperatorTruncationPayload> {
  return {
    version: 1,
    conversationId: "conversation-1",
    requestId: "request-1",
    sequence,
    type: "truncation",
    payload: {
      reason: "frames_per_turn",
      limits: {
        maxFramesPerTurn: 2000,
        maxTurnPayloadBytes: 8388608,
        maxFramePayloadBytes: 65536,
      },
      elidedFrames: 4,
      elidedBytes: 2048,
      elidedFrameTypes: { text: 4 },
      firstElidedAt: 1_700_000_000,
      lastElidedAt: 1_700_000_020,
      message:
        "This turn reached its durable retention limit; frames after this point were not stored and are counted here.",
    },
    createdAt: 1_700_000_020,
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

  it("shows a retained turn's truncation notice and its terminal outcome together", async () => {
    window.localStorage.setItem("studio:operator-conversation", "conversation-1");
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-1",
        title: "Long turn",
        status: "active",
        activeRequestId: null,
        updatedAt: 3,
      },
    ]);
    api.getOperatorConversation.mockResolvedValue({
      conversation: {
        id: "conversation-1",
        status: "active",
        activeRequestId: null,
      },
      frames: [
        textFrame(1, "assistant", "Retained before the limit."),
        truncationFrame(2),
        {
          version: 1,
          conversationId: "conversation-1",
          requestId: "request-1",
          sequence: 3,
          type: "done",
          payload: { outcome: "completed", lastSequence: 3 },
          createdAt: 3,
        },
      ],
    });

    await mount();

    // The notice states what was not retained, in frames and bytes, and says
    // the retained transcript is not the whole turn.
    expect(container.textContent).toContain("Retained before the limit.");
    expect(container.textContent).toContain("4 frames not retained");
    expect(container.textContent).toContain("2 KB");
    expect(container.textContent).toContain("frames_per_turn");
    // The transcript did not continue: the notice must not read as an
    // ordinary message between two pieces of the same turn.
    expect(container.textContent).not.toContain("This turn reached its durable retention limit");
    expect(container.textContent).toMatch(/Turn completed/);
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
      let reject!: (error: Error) => void;
      const promise = new Promise<T>((complete, fail) => {
        resolve = complete;
        reject = fail;
      });
      return { promise, resolve, reject };
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

    const firstRow = {
      id: "conversation-1",
      title: "Scheduler check",
      status: "active",
      pinned: false,
      activeRequestId: null,
    } satisfies OperatorConversation;
    const archivedRow: OperatorConversation = {
      ...firstRow,
      id: "archived-1",
      title: "Archived conversation",
      status: "archived",
    };

    async function clickFilter(label: "Active" | "Archived") {
      const button = [...container.querySelectorAll("button")].find(
        (item) => item.textContent === label,
      )!;
      await act(async () => button.click());
    }

    async function typeInstruction(text: string) {
      const input = container.querySelector("textarea")!;
      await act(async () => {
        Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")!.set!.call(
          input,
          text,
        );
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    }

    async function sendInstruction() {
      await typeInstruction("Inspect this project");
      const send = [...container.querySelectorAll("button")].find(
        (button) => button.textContent === "Send",
      )!;
      await act(async () => send.click());
    }

    it("uses fresh server rows and removes rows absent from a later list read", async () => {
      const other = { ...firstRow, id: "other", title: "Externally removed" };
      api.listOperatorConversations.mockResolvedValueOnce([firstRow, other]);
      await mount();
      await openList();
      expect(container.querySelector("ul")?.textContent).toContain(other.title);
      api.listOperatorConversations.mockResolvedValueOnce([
        { ...firstRow, title: "Externally renamed", pinned: true },
      ]);
      await clickFilter("Active");
      expect(container.querySelector("ul")?.textContent).toContain("★ Externally renamed");
      expect(container.querySelector("ul")?.textContent).not.toContain(firstRow.title);
      expect(container.querySelector("ul")?.textContent).not.toContain(other.title);
    });

    it("does not preserve a local creation that predates a later list read", async () => {
      api.listOperatorConversations.mockResolvedValueOnce([]);
      api.createOperatorConversation.mockResolvedValueOnce(firstRow);
      api.submitOperatorTurn.mockResolvedValueOnce({ requestId: "created-turn" });
      await mount();
      await sendInstruction();
      await openList();
      expect(container.querySelector("ul")?.textContent).toContain(firstRow.title);
      api.listOperatorConversations.mockResolvedValueOnce([]);
      await clickFilter("Active");
      expect(container.querySelector("ul")?.textContent).not.toContain(firstRow.title);
    });

    it("does not resurrect a row archived while a list read was pending", async () => {
      await mount();
      await openList();
      const pending = deferred<OperatorConversation[]>();
      api.listOperatorConversations.mockReturnValueOnce(pending.promise);
      await clickFilter("Active");
      api.updateOperatorConversation.mockResolvedValueOnce({ ...firstRow, status: "archived" });
      const archive = container.querySelector(
        'ul li button[aria-label="Archive"]',
      ) as HTMLButtonElement;
      await act(async () => archive.click());
      expect(container.querySelector("ul")?.textContent).not.toContain(firstRow.title);
      await act(async () => pending.resolve([firstRow]));
      expect(container.querySelector("ul")?.textContent).not.toContain(firstRow.title);
      // A subsequent authoritative read can still observe an external unarchive.
      api.listOperatorConversations.mockResolvedValueOnce([firstRow]);
      await clickFilter("Active");
      expect(container.querySelector("ul")?.textContent).toContain(firstRow.title);
    });

    it.each([
      [50, false],
      [100, false],
      [200, true],
    ])(
      "against an archive at 100, a pending read's row from %i is shown: %s",
      async (snapshotAt, shown) => {
        await mount();
        await openList();
        const pending = deferred<OperatorConversation[]>();
        api.listOperatorConversations.mockReturnValueOnce(pending.promise);
        await clickFilter("Active");
        api.updateOperatorConversation.mockResolvedValueOnce({
          ...firstRow,
          status: "archived",
          updatedAt: 100,
        });
        const archive = container.querySelector(
          'ul li button[aria-label="Archive"]',
        ) as HTMLButtonElement;
        await act(async () => archive.click());
        expect(container.querySelector("ul")?.textContent).not.toContain(firstRow.title);
        // The server unarchived the row after the archive and before this snapshot.
        await act(async () => pending.resolve([{ ...firstRow, updatedAt: snapshotAt }]));
        expect(container.querySelector("ul")?.textContent?.includes(firstRow.title)).toBe(shown);
      },
    );

    it.each(["initial", "manual"])(
      "ignores the %s list result after its filter changes",
      async (source) => {
        const pending = deferred<OperatorConversation[]>();
        if (source === "initial")
          api.listOperatorConversations.mockReturnValueOnce(pending.promise);
        await mount();
        await openList();
        if (source === "manual") {
          api.listOperatorConversations.mockReturnValueOnce(pending.promise);
          await clickFilter("Active");
        }
        api.listOperatorConversations.mockResolvedValueOnce([archivedRow]);
        await clickFilter("Archived");
        await act(async () => pending.resolve([firstRow]));
        expect(container.querySelector("ul")?.textContent).toContain(archivedRow.title);
        expect(container.querySelector("ul")?.textContent).not.toContain(firstRow.title);
      },
    );

    it("keeps the current filter when a selected conversation load settles", async () => {
      const pending = deferred<{ conversation: OperatorConversation; frames: OperatorFrame[] }>();
      api.getOperatorConversation.mockReturnValueOnce(pending.promise);
      await mount();
      await openList();
      api.listOperatorConversations.mockResolvedValueOnce([archivedRow]);
      await clickFilter("Archived");
      await act(async () => pending.resolve({ conversation: firstRow, frames: [] }));
      expect(container.querySelector("ul")?.textContent).toContain(archivedRow.title);
      expect(container.querySelector("ul")?.textContent).not.toContain(firstRow.title);
    });

    it("uses the live filter when an archive response settles", async () => {
      await mount();
      await openList();
      const pending = deferred<OperatorConversation>();
      api.updateOperatorConversation.mockReturnValueOnce(pending.promise);
      const archive = container.querySelector(
        'ul li button[aria-label="Archive"]',
      ) as HTMLButtonElement;
      await act(async () => archive.click());
      api.listOperatorConversations.mockResolvedValueOnce([{ ...firstRow, status: "archived" }]);
      await clickFilter("Archived");
      await act(async () => pending.resolve({ ...firstRow, status: "archived" }));
      expect(container.querySelector("ul")?.textContent).toContain(firstRow.title);
      expect(container.querySelector('ul li button[aria-label="Unarchive"]')).not.toBeNull();
    });

    it("ignores a list error after its filter changes", async () => {
      await mount();
      await openList();
      const pending = deferred<OperatorConversation[]>();
      api.listOperatorConversations.mockReturnValueOnce(pending.promise);
      await clickFilter("Active");
      api.listOperatorConversations.mockResolvedValueOnce([archivedRow]);
      await clickFilter("Archived");
      await act(async () => pending.reject(new Error("Old filter failed")));
      expect(container.textContent).not.toContain("Old filter failed");
      expect(container.querySelector("ul")?.textContent).toContain(archivedRow.title);
    });

    it.each([false, true])(
      "ignores a failed mount list after local creation (stored=%s)",
      async (stored) => {
        if (stored) window.localStorage.setItem("studio:operator-conversation", "stored-old");
        const pending = deferred<OperatorConversation[]>();
        api.listOperatorConversations.mockReturnValueOnce(pending.promise);
        api.createOperatorConversation.mockResolvedValueOnce(firstRow);
        api.submitOperatorTurn.mockResolvedValueOnce({ requestId: "created-turn" });
        await mount();
        await sendInstruction();
        await act(async () => pending.reject(new Error("Mount list failed")));
        expect(api.getOperatorConversation).not.toHaveBeenCalled();
        expect(window.localStorage.getItem("studio:operator-conversation")).toBe(firstRow.id);
        expect(container.querySelector('[role="alert"]')).toBeNull();
        expect(container.textContent).not.toContain(enMessages.operator.errors.load);
      },
    );

    it.each(["selection", "unmount"])(
      "does not reload stale_context after %s changes",
      async (change) => {
        const other = { ...firstRow, id: "other", title: "Other conversation" };
        api.listOperatorConversations.mockResolvedValueOnce([firstRow, other]);
        const pending = deferred<{ requestId: string }>();
        api.submitOperatorTurn.mockReturnValueOnce(pending.promise);
        await mount();
        await sendInstruction();
        if (change === "selection") {
          await openList();
          api.getOperatorConversation.mockResolvedValueOnce({ conversation: other, frames: [] });
          const select = [...container.querySelectorAll("ul li button")].find(
            (button) => button.textContent === other.title,
          ) as HTMLButtonElement;
          await act(async () => select.click());
        } else {
          await act(async () => root?.unmount());
          root = null;
        }
        const calls = api.getOperatorConversation.mock.calls.length;
        await act(async () =>
          pending.reject(new ApiError(409, "Old context", { code: "stale_context" })),
        );
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(calls);
        if (change === "selection") expect(container.textContent).not.toContain("Old context");
        expect(window.localStorage.getItem("studio:operator-conversation")).toBe(
          change === "selection" ? other.id : firstRow.id,
        );
      },
    );

    it("reports a failed draft send in the conversation created for it", async () => {
      api.listOperatorConversations.mockResolvedValueOnce([]);
      api.createOperatorConversation.mockResolvedValueOnce(firstRow);
      api.submitOperatorTurn.mockRejectedValueOnce(new Error("Draft send failed"));
      await mount();
      await sendInstruction();

      expect(container.textContent).toContain("Draft send failed");
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(firstRow.id);
    });

    it("reports a failed send and its text when its conversation is reselected", async () => {
      const other = { ...firstRow, id: "other", title: "Other conversation" };
      api.listOperatorConversations.mockResolvedValueOnce([]);
      api.createOperatorConversation.mockResolvedValueOnce(firstRow);
      const pending = deferred<{ requestId: string }>();
      api.submitOperatorTurn.mockReturnValueOnce(pending.promise);
      await mount();
      await sendInstruction();
      await openList();
      api.listOperatorConversations.mockResolvedValueOnce([firstRow, other]);
      await clickFilter("Active");
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: other, frames: [] });
      const select = [...container.querySelectorAll("ul li button")].find(
        (button) => button.textContent === other.title,
      ) as HTMLButtonElement;
      await act(async () => select.click());

      await act(async () => pending.reject(new Error("Draft send failed")));

      expect(container.textContent).not.toContain("Draft send failed");
      expect(container.querySelector('[role="alert"]')).toBeNull();
      expect(container.querySelector("textarea")?.value).toBe("");
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(other.id);
      await typeInstruction("Second draft");

      async function reselect(row: typeof firstRow) {
        await openList();
        const button = [...container.querySelectorAll("ul li button")].find(
          (item) => item.textContent === row.title,
        ) as HTMLButtonElement;
        await act(async () => button.click());
      }
      await reselect(firstRow);

      // The failed text comes back in front of what was typed since; neither is lost.
      expect(container.querySelector('[role="alert"]')?.textContent).toContain("Draft send failed");
      expect(container.querySelector("textarea")?.value).toBe("Inspect this project\nSecond draft");
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(firstRow.id);

      // Reported once: leaving and selecting it again shows no alert.
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: other, frames: [] });
      await reselect(other);
      await reselect(firstRow);
      expect(container.querySelector('[role="alert"]')).toBeNull();
    });

    it("reports a draft whose conversation could not be created where it is selected", async () => {
      const other = { ...firstRow, id: "other", title: "Other conversation" };
      api.listOperatorConversations.mockResolvedValueOnce([]);
      const pending = deferred<OperatorConversation>();
      api.createOperatorConversation.mockReturnValueOnce(pending.promise);
      const submits = api.submitOperatorTurn.mock.calls.length;
      await mount();
      await sendInstruction();
      await openList();
      api.listOperatorConversations.mockResolvedValueOnce([other]);
      await clickFilter("Active");
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: other, frames: [] });
      const select = [...container.querySelectorAll("ul li button")].find(
        (button) => button.textContent === other.title,
      ) as HTMLButtonElement;
      await act(async () => select.click());

      await act(async () => pending.reject(new Error("Draft create failed")));

      expect(container.textContent).toContain("Draft create failed");
      expect(container.querySelector("textarea")?.value).toBe("Inspect this project");
      expect(api.submitOperatorTurn).toHaveBeenCalledTimes(submits);
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(other.id);
    });

    it("reports a draft sent while a conversation loads where it is selected", async () => {
      const other = { ...firstRow, id: "other", title: "Other conversation" };
      api.listOperatorConversations.mockResolvedValueOnce([firstRow, other]);
      const loading = deferred<{ conversation: OperatorConversation; frames: OperatorFrame[] }>();
      api.getOperatorConversation.mockReturnValueOnce(loading.promise);
      const pending = deferred<OperatorConversation>();
      api.createOperatorConversation.mockReturnValueOnce(pending.promise);
      const creates = api.createOperatorConversation.mock.calls.length;
      await mount();
      // The first row is still loading, so the send has no conversation and creates one.
      await sendInstruction();
      expect(api.createOperatorConversation).toHaveBeenCalledTimes(creates + 1);
      await openList();
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: other, frames: [] });
      const select = [...container.querySelectorAll("ul li button")].find(
        (button) => button.textContent === other.title,
      ) as HTMLButtonElement;
      await act(async () => select.click());

      await act(async () => pending.reject(new Error("Draft create failed")));

      expect(container.querySelector('[role="alert"]')?.textContent).toContain(
        "Draft create failed",
      );
      expect(container.querySelector("textarea")?.value).toBe("Inspect this project");

      // Nothing was kept for the row that was loading, so selecting it shows no alert.
      await act(async () => loading.resolve({ conversation: firstRow, frames: [] }));
      await openList();
      const back = [...container.querySelectorAll("ul li button")].find(
        (button) => button.textContent === firstRow.title,
      ) as HTMLButtonElement;
      await act(async () => back.click());
      expect(container.querySelector('[role="alert"]')).toBeNull();
      expect(container.querySelector("textarea")?.value).toBe("Inspect this project");
    });

    function buttonNamed(name: string) {
      return [...container.querySelectorAll("button")].find(
        (button) => button.textContent === name,
      );
    }

    async function selectRow(row: { title: string }) {
      await openList();
      const button = [...container.querySelectorAll("ul li button")].find(
        (item) => item.textContent === row.title,
      ) as HTMLButtonElement;
      await act(async () => button.click());
    }

    it("keeps a conversation chosen before a draft's conversation is created", async () => {
      const other = { ...firstRow, id: "other", title: "Other conversation" };
      const created = { ...firstRow, id: "created-1", title: "New conversation" };
      api.listOperatorConversations.mockResolvedValueOnce([firstRow, other]);
      const loading = deferred<{ conversation: OperatorConversation; frames: OperatorFrame[] }>();
      api.getOperatorConversation.mockReturnValueOnce(loading.promise);
      const pending = deferred<OperatorConversation>();
      api.createOperatorConversation.mockReturnValueOnce(pending.promise);
      api.submitOperatorTurn.mockResolvedValueOnce({ requestId: "request-created" });
      const submits = api.submitOperatorTurn.mock.calls.length;
      await mount();
      await sendInstruction();
      const otherLoad = deferred<{ conversation: OperatorConversation; frames: OperatorFrame[] }>();
      api.getOperatorConversation.mockReturnValueOnce(otherLoad.promise);
      await selectRow(other);

      // The create settles while the chosen conversation is still loading.
      await act(async () => pending.resolve(created));
      await act(async () => otherLoad.resolve({ conversation: other, frames: [] }));
      await act(async () => loading.resolve({ conversation: firstRow, frames: [] }));

      expect(api.submitOperatorTurn).toHaveBeenCalledTimes(submits + 1);
      expect(api.submitOperatorTurn.mock.calls[submits][0]).toBe(created.id);
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(other.id);
      await openList();
      expect(container.querySelector("ul")?.textContent).toContain(created.title);
    });

    it("keeps a loaded conversation chosen before a draft's conversation is created", async () => {
      const other = { ...firstRow, id: "other", title: "Other conversation" };
      const created = { ...firstRow, id: "created-1", title: "New conversation" };
      api.listOperatorConversations.mockResolvedValueOnce([firstRow, other]);
      const loading = deferred<{ conversation: OperatorConversation; frames: OperatorFrame[] }>();
      api.getOperatorConversation.mockReturnValueOnce(loading.promise);
      const pending = deferred<OperatorConversation>();
      api.createOperatorConversation.mockReturnValueOnce(pending.promise);
      api.submitOperatorTurn.mockResolvedValueOnce({ requestId: "request-created" });
      const submits = api.submitOperatorTurn.mock.calls.length;
      await mount();
      await sendInstruction();
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: other, frames: [] });
      await selectRow(other);

      // The chosen conversation is shown before the create settles.
      await act(async () => pending.resolve(created));

      expect(api.submitOperatorTurn.mock.calls[submits][0]).toBe(created.id);
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(other.id);
      // The accepted turn runs in the new conversation; the chosen one shows no Stop.
      expect(buttonNamed("Stop")).toBeUndefined();
      expect(buttonNamed("Send")).toBeDefined();
    });

    it("lets a draft's conversation take the panel when the list selects a row by itself", async () => {
      const created = { ...firstRow, id: "created-1", title: "New conversation" };
      const listing = deferred<OperatorConversation[]>();
      api.listOperatorConversations.mockReturnValueOnce(listing.promise);
      const pending = deferred<OperatorConversation>();
      api.createOperatorConversation.mockReturnValueOnce(pending.promise);
      api.submitOperatorTurn.mockResolvedValueOnce({ requestId: "request-created" });
      await mount();
      await sendInstruction();
      // The list read settles during the create and selects its first row; the
      // operator chose nothing.
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: firstRow, frames: [] });
      await act(async () => listing.resolve([firstRow]));
      await act(async () => pending.resolve(created));

      expect(api.submitOperatorTurn.mock.calls.at(-1)?.[0]).toBe(created.id);
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(created.id);
      expect(buttonNamed("Stop")).toBeDefined();
    });

    it("keeps a conversation the operator chose again while a draft's conversation is created", async () => {
      const created = { ...firstRow, id: "created-1", title: "New conversation" };
      api.listOperatorConversations.mockResolvedValueOnce([firstRow]);
      const loading = deferred<{ conversation: OperatorConversation; frames: OperatorFrame[] }>();
      api.getOperatorConversation.mockReturnValueOnce(loading.promise);
      const pending = deferred<OperatorConversation>();
      api.createOperatorConversation.mockReturnValueOnce(pending.promise);
      api.submitOperatorTurn.mockResolvedValueOnce({ requestId: "request-created" });
      await mount();
      await sendInstruction();
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: firstRow, frames: [] });
      await selectRow(firstRow);

      await act(async () => pending.resolve(created));
      await act(async () => loading.resolve({ conversation: firstRow, frames: [] }));

      expect(api.submitOperatorTurn.mock.calls.at(-1)?.[0]).toBe(created.id);
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(firstRow.id);
      expect(buttonNamed("Stop")).toBeUndefined();
    });

    it("reports a kept failure after a send still in flight settles", async () => {
      const other = { ...firstRow, id: "other", title: "Other conversation" };
      api.listOperatorConversations.mockResolvedValueOnce([]);
      api.createOperatorConversation.mockResolvedValueOnce(firstRow);
      const pending = deferred<{ requestId: string }>();
      api.submitOperatorTurn.mockReturnValueOnce(pending.promise);
      await mount();
      await sendInstruction();
      await openList();
      api.listOperatorConversations.mockResolvedValueOnce([firstRow, other]);
      await clickFilter("Active");
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: other, frames: [] });
      const select = [...container.querySelectorAll("ul li button")].find(
        (button) => button.textContent === other.title,
      ) as HTMLButtonElement;
      await act(async () => select.click());
      await act(async () => pending.reject(new Error("Draft send failed")));

      // Back to the failed conversation; a draft is sent while it loads.
      const created = { ...firstRow, id: "created-2", title: "New conversation" };
      const again = deferred<{ conversation: OperatorConversation; frames: OperatorFrame[] }>();
      api.getOperatorConversation.mockReturnValueOnce(again.promise);
      await selectRow(firstRow);
      const creating = deferred<OperatorConversation>();
      api.createOperatorConversation.mockReturnValueOnce(creating.promise);
      api.submitOperatorTurn.mockResolvedValueOnce({ requestId: "request-2" });
      await typeInstruction("Second draft");
      const send = [...container.querySelectorAll("button")].find(
        (button) => button.textContent === "Send",
      )!;
      await act(async () => send.click());

      await act(async () => again.resolve({ conversation: firstRow, frames: [] }));
      // Not reported while that send still holds the composer.
      expect(container.querySelector('[role="alert"]')).toBeNull();
      expect(container.querySelector("textarea")?.value).toBe("Second draft");

      await act(async () => creating.resolve(created));
      // The draft went to its new conversation, which shows neither the failure nor its text.
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(created.id);
      expect(container.querySelector('[role="alert"]')).toBeNull();
      expect(container.querySelector("textarea")?.value).toBe("");

      await selectRow(firstRow);
      expect(container.querySelector('[role="alert"]')?.textContent).toContain("Draft send failed");
      expect(container.querySelector("textarea")?.value).toBe("Inspect this project");
    });

    function proposalFrame(conversationId: string, id: string): OperatorFrame {
      return {
        version: 1,
        conversationId,
        requestId: `request-${id}`,
        sequence: 1,
        type: "proposal",
        payload: {
          proposal: {
            id,
            command: { operation: "shell", argv: ["git", "status"] },
            commandHash: "sha256",
            risk: "execute",
            summary: "Inspect repository state",
            idempotencyKey: id,
            expiresAt: 999,
          },
        },
        createdAt: 1,
      } as unknown as OperatorFrame;
    }

    // A send on the first row fails while another conversation is shown, so its
    // failure is kept. A decision on that other conversation is still in flight when
    // the first row is chosen again, and it fails while the row loads, so its error
    // is showing when the row becomes ready.
    async function keepAFailureBehindADecisionError(reload: {
      conversation: OperatorConversation;
      frames: OperatorFrame[];
    }) {
      const other = { ...firstRow, id: "other", title: "Other conversation" };
      api.listOperatorConversations.mockResolvedValueOnce([firstRow, other]);
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: firstRow, frames: [] });
      const pending = deferred<{ requestId: string }>();
      api.submitOperatorTurn.mockReturnValueOnce(pending.promise);
      await mount();
      await sendInstruction();
      api.getOperatorConversation.mockResolvedValueOnce({
        conversation: other,
        frames: [proposalFrame(other.id, "proposal-1")],
      });
      await selectRow(other);
      await act(async () => pending.reject(new Error("Send failed on the first row")));

      const decision = deferred<{ proposalId: string; status: string }>();
      api.decideOperatorProposal.mockReturnValueOnce(decision.promise);
      await act(async () => buttonNamed("Allow")!.click());
      const reloading = deferred<{ conversation: OperatorConversation; frames: OperatorFrame[] }>();
      api.getOperatorConversation.mockReturnValueOnce(reloading.promise);
      await selectRow(firstRow);
      await act(async () => decision.reject(new Error("First decision failed")));
      const draft = container.querySelector("textarea")!.value;
      await act(async () => reloading.resolve(reload));
      return { other, draft };
    }

    async function expectTheKeptFailureOnReturn(other: OperatorConversation) {
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: other, frames: [] });
      await selectRow(other);
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: firstRow, frames: [] });
      await selectRow(firstRow);
      expect(container.querySelector('[role="alert"]')?.textContent).toContain(
        "Send failed on the first row",
      );
      expect(container.querySelector("textarea")?.value).toBe("Inspect this project");
    }

    it("leaves a decision error showing when a kept failure's conversation loads", async () => {
      const { other, draft } = await keepAFailureBehindADecisionError({
        conversation: firstRow,
        frames: [],
      });

      expect(container.querySelector('[role="alert"]')?.textContent).toContain(
        "First decision failed",
      );
      expect(container.textContent).not.toContain("Send failed on the first row");
      expect(container.querySelector("textarea")?.value).toBe(draft);

      await expectTheKeptFailureOnReturn(other);
    });

    it.each(["decision", "stop"])(
      "keeps a failure kept behind an alert while a %s started over it is in flight",
      async (action) => {
        const { other, draft } = await keepAFailureBehindADecisionError(
          action === "decision"
            ? { conversation: firstRow, frames: [proposalFrame(firstRow.id, "proposal-2")] }
            : { conversation: { ...firstRow, activeRequestId: "request-running" }, frames: [] },
        );
        const inFlight = deferred<never>();
        if (action === "decision") {
          api.decideOperatorProposal.mockReturnValueOnce(inFlight.promise);
          await act(async () => buttonNamed("Allow")!.click());
        } else {
          api.cancelOperatorRequest.mockReturnValueOnce(inFlight.promise);
          await act(async () => buttonNamed("Stop")!.click());
        }

        // Starting it cleared the alert; the kept failure waits for its outcome.
        expect(container.querySelector('[role="alert"]')).toBeNull();
        expect(container.querySelector("textarea")?.value).toBe(draft);

        await act(async () => inFlight.reject(new Error(`The ${action} failed`)));
        expect(container.querySelector('[role="alert"]')?.textContent).toContain(
          `The ${action} failed`,
        );
        expect(container.textContent).not.toContain("Send failed on the first row");

        await expectTheKeptFailureOnReturn(other);
      },
    );

    async function flushAutoAllow() {
      // Auto-allow decides on a zero-delay timer; let it and the decision settle.
      for (let tick = 0; tick < 3; tick += 1) {
        await act(async () => {
          await new Promise((settle) => window.setTimeout(settle, 0));
        });
      }
    }

    async function startANewConversation() {
      await openList();
      const button = [...container.querySelectorAll("button")].find(
        (item) =>
          item.textContent === "Start a new conversation" && !item.hasAttribute("aria-expanded"),
      ) as HTMLButtonElement;
      await act(async () => button.click());
    }

    it("reports a kept failure once auto-allow has decided its conversation's proposal", async () => {
      window.localStorage.setItem("studio:operator-auto-allow", "1");
      const other = { ...firstRow, id: "other", title: "Other conversation" };
      api.listOperatorConversations.mockResolvedValueOnce([firstRow, other]);
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: firstRow, frames: [] });
      const pending = deferred<{ requestId: string }>();
      api.submitOperatorTurn.mockReturnValueOnce(pending.promise);
      await mount();
      await sendInstruction();
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: other, frames: [] });
      await selectRow(other);
      await act(async () => pending.reject(new Error("Send failed on the first row")));

      api.getOperatorConversation.mockResolvedValueOnce({
        conversation: firstRow,
        frames: [proposalFrame(firstRow.id, "proposal-1")],
      });
      await selectRow(firstRow);
      await flushAutoAllow();

      expect(api.decideOperatorProposal).toHaveBeenCalledTimes(1);
      expect(container.querySelector('[role="alert"]')?.textContent).toContain(
        "Send failed on the first row",
      );
      expect(container.querySelector("textarea")?.value).toBe("Inspect this project");
    });

    async function keepAFailureForTheFirstRow() {
      const other = { ...firstRow, id: "other", title: "Other conversation" };
      api.listOperatorConversations.mockResolvedValueOnce([firstRow, other]);
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: firstRow, frames: [] });
      const pending = deferred<{ requestId: string }>();
      api.submitOperatorTurn.mockReturnValueOnce(pending.promise);
      await mount();
      await sendInstruction();
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: other, frames: [] });
      await selectRow(other);
      await act(async () => pending.reject(new Error("Send failed on the first row")));
      return other;
    }

    it("leaves a reported failure showing when auto-allow decides a proposal that arrives after it", async () => {
      window.localStorage.setItem("studio:operator-auto-allow", "1");
      const subscriptions: StreamHandlers[] = [];
      api.streamOperatorConversation.mockImplementation(
        (_conversationId: string, _afterSequence: number, handlers: StreamHandlers) => {
          subscriptions.push(handlers);
          return vi.fn();
        },
      );
      await keepAFailureForTheFirstRow();
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: firstRow, frames: [] });
      await selectRow(firstRow);
      expect(container.querySelector('[role="alert"]')?.textContent).toContain(
        "Send failed on the first row",
      );

      await act(async () =>
        subscriptions.at(-1)!.onFrame(proposalFrame(firstRow.id, "proposal-1")),
      );
      await flushAutoAllow();

      expect(api.decideOperatorProposal).toHaveBeenCalledTimes(1);
      expect(container.querySelector('[role="alert"]')?.textContent).toContain(
        "Send failed on the first row",
      );
    });

    it("keeps a kept failure when auto-allow's decision on its conversation is refused", async () => {
      window.localStorage.setItem("studio:operator-auto-allow", "1");
      const other = await keepAFailureForTheFirstRow();
      api.decideOperatorProposal.mockRejectedValueOnce(new Error("Decision refused"));
      api.getOperatorConversation.mockResolvedValueOnce({
        conversation: firstRow,
        frames: [proposalFrame(firstRow.id, "proposal-1")],
      });
      await selectRow(firstRow);
      await flushAutoAllow();

      expect(container.querySelector('[role="alert"]')?.textContent).toContain("Decision refused");
      expect(container.textContent).not.toContain("Send failed on the first row");

      await expectTheKeptFailureOnReturn(other);
    });

    it("sends a refused auto-allow decision once and leaves its error showing", async () => {
      window.localStorage.setItem("studio:operator-auto-allow", "1");
      api.listOperatorConversations.mockResolvedValueOnce([firstRow]);
      api.getOperatorConversation.mockResolvedValueOnce({
        conversation: firstRow,
        frames: [proposalFrame(firstRow.id, "proposal-1")],
      });
      api.decideOperatorProposal.mockRejectedValue(new Error("Decision refused"));
      await mount();
      await flushAutoAllow();
      await flushAutoAllow();

      expect(api.decideOperatorProposal).toHaveBeenCalledTimes(1);
      expect(container.querySelector('[role="alert"]')?.textContent).toContain("Decision refused");

      api.decideOperatorProposal.mockResolvedValueOnce({
        proposalId: "proposal-1",
        status: "allowed",
      });
      await act(async () => buttonNamed("Allow")!.click());
      expect(api.decideOperatorProposal).toHaveBeenCalledTimes(2);
    });

    it("decides a proposal appended after New conversation once its conversation is shown again", async () => {
      window.localStorage.setItem("studio:operator-auto-allow", "1");
      const subscriptions: StreamHandlers[] = [];
      api.streamOperatorConversation.mockImplementation(
        (_conversationId: string, _afterSequence: number, handlers: StreamHandlers) => {
          subscriptions.push(handlers);
          return vi.fn();
        },
      );
      // Frames wait for an animation frame that never runs, so the stream's cleanup
      // appends this one after New conversation has cleared the panel.
      vi.stubGlobal("requestAnimationFrame", () => 1);
      api.listOperatorConversations.mockResolvedValueOnce([firstRow]);
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: firstRow, frames: [] });
      await mount();
      act(() => subscriptions.at(-1)!.onFrame(proposalFrame(firstRow.id, "proposal-1")));
      await startANewConversation();
      await flushAutoAllow();
      expect(api.decideOperatorProposal).not.toHaveBeenCalled();

      api.getOperatorConversation.mockResolvedValueOnce({
        conversation: firstRow,
        frames: [proposalFrame(firstRow.id, "proposal-1")],
      });
      await selectRow(firstRow);
      await flushAutoAllow();

      expect(api.decideOperatorProposal).toHaveBeenCalledTimes(1);
    });

    it("keeps a new conversation started before the first list read settles", async () => {
      const listing = deferred<OperatorConversation[]>();
      api.listOperatorConversations.mockReturnValueOnce(listing.promise);
      await mount();
      await startANewConversation();
      const loads = api.getOperatorConversation.mock.calls.length;

      await act(async () => listing.resolve([firstRow]));

      expect(api.getOperatorConversation).toHaveBeenCalledTimes(loads);
      expect(window.localStorage.getItem("studio:operator-conversation")).toBeNull();
    });

    it("keeps a new conversation started before the first list read fails", async () => {
      window.localStorage.setItem("studio:operator-conversation", firstRow.id);
      const listing = deferred<OperatorConversation[]>();
      api.listOperatorConversations.mockReturnValueOnce(listing.promise);
      await mount();
      await startANewConversation();
      const loads = api.getOperatorConversation.mock.calls.length;

      await act(async () => listing.reject(new Error("List failed")));

      expect(api.getOperatorConversation).toHaveBeenCalledTimes(loads);
      expect(container.querySelector('[role="alert"]')).toBeNull();
    });

    it("keeps a draft's text when its conversation cannot be created after a new conversation", async () => {
      api.listOperatorConversations.mockResolvedValueOnce([]);
      const pending = deferred<OperatorConversation>();
      api.createOperatorConversation.mockReturnValueOnce(pending.promise);
      await mount();
      await sendInstruction();
      await startANewConversation();
      expect(container.querySelector("textarea")?.value).toBe("");

      await act(async () => pending.reject(new Error("Draft create failed")));

      expect(container.querySelector('[role="alert"]')?.textContent).toContain(
        "Draft create failed",
      );
      expect(container.querySelector("textarea")?.value).toBe("Inspect this project");
    });

    it("keeps a new conversation started while a draft's conversation is created", async () => {
      const created = { ...firstRow, id: "created-1", title: "New conversation" };
      api.listOperatorConversations.mockResolvedValueOnce([]);
      const pending = deferred<OperatorConversation>();
      api.createOperatorConversation.mockReturnValueOnce(pending.promise);
      api.submitOperatorTurn.mockResolvedValueOnce({ requestId: "request-created" });
      await mount();
      await sendInstruction();
      await startANewConversation();

      await act(async () => pending.resolve(created));

      expect(api.submitOperatorTurn.mock.calls.at(-1)?.[0]).toBe(created.id);
      expect(window.localStorage.getItem("studio:operator-conversation")).toBeNull();
      expect(buttonNamed("Stop")).toBeUndefined();
    });

    it("keeps a conversation retried while a draft's conversation is created", async () => {
      const created = { ...firstRow, id: "created-1", title: "New conversation" };
      api.listOperatorConversations.mockResolvedValueOnce([firstRow]);
      const loading = deferred<{ conversation: OperatorConversation; frames: OperatorFrame[] }>();
      api.getOperatorConversation.mockReturnValueOnce(loading.promise);
      const pending = deferred<OperatorConversation>();
      api.createOperatorConversation.mockReturnValueOnce(pending.promise);
      api.submitOperatorTurn.mockResolvedValueOnce({ requestId: "request-created" });
      await mount();
      // Sent while the list's own selection loads; that load then fails.
      await sendInstruction();
      await act(async () => loading.reject(new Error("Load failed")));
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: firstRow, frames: [] });
      await act(async () => buttonNamed("Retry")!.click());

      await act(async () => pending.resolve(created));

      expect(api.submitOperatorTurn.mock.calls.at(-1)?.[0]).toBe(created.id);
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(firstRow.id);
      expect(buttonNamed("Stop")).toBeUndefined();
    });

    it("retries the conversation whose load failed, not the stored one", async () => {
      const other = { ...firstRow, id: "other", title: "Other conversation" };
      window.localStorage.setItem("studio:operator-conversation", firstRow.id);
      api.listOperatorConversations.mockResolvedValueOnce([firstRow, other]);
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: firstRow, frames: [] });
      await mount();
      api.getOperatorConversation.mockRejectedValueOnce(new Error("Load failed"));
      await selectRow(other);
      expect(buttonNamed("Retry")).toBeDefined();

      api.getOperatorConversation.mockResolvedValueOnce({ conversation: other, frames: [] });
      await act(async () => buttonNamed("Retry")!.click());

      expect(api.getOperatorConversation.mock.calls.at(-1)?.[0]).toBe(other.id);
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(other.id);
    });

    it("keeps a fork when a draft's conversation is created during the fork request", async () => {
      const created = { ...firstRow, id: "created-1", title: "New conversation" };
      const forked = { ...firstRow, id: "conversation-fork", title: "Scheduler check (fork)" };
      api.listOperatorConversations.mockResolvedValueOnce([firstRow]);
      api.getOperatorConversation.mockReturnValueOnce(new Promise(() => {}));
      const pending = deferred<OperatorConversation>();
      api.createOperatorConversation.mockReturnValueOnce(pending.promise);
      api.submitOperatorTurn.mockResolvedValueOnce({ requestId: "request-created" });
      const forking = deferred<{ conversation: OperatorConversation; frames: OperatorFrame[] }>();
      api.forkOperatorConversation.mockReturnValueOnce(forking.promise);
      await mount();
      await sendInstruction();
      await openList();
      const forkButton = container.querySelector(
        'ul li button[aria-label="Fork"]',
      ) as HTMLButtonElement;
      await act(async () => forkButton.click());

      // The draft's conversation is created while the fork request is in flight.
      await act(async () => pending.resolve(created));
      expect(window.localStorage.getItem("studio:operator-conversation")).not.toBe(created.id);
      expect(buttonNamed("Stop")).toBeUndefined();

      await act(async () => forking.resolve({ conversation: forked, frames: [] }));
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(forked.id);
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

      it("uses fresh nonselected rows during metadata refresh", async () => {
        const { conversation, other, emit } = await mountMetadataConversation();
        api.getOperatorConversation.mockResolvedValueOnce({ conversation, frames: [] });
        api.listOperatorConversations.mockResolvedValueOnce([
          conversation,
          { ...other, title: "External update" },
        ]);
        await emit(metadataCall(1), metadataResult(2));
        expect(container.querySelector("ul")?.textContent).toContain("External update");
        expect(container.querySelector("ul")?.textContent).not.toContain(other.title);
      });

      it("keeps the current filter when an older metadata refresh settles", async () => {
        const { conversation, other, emit } = await mountMetadataConversation();
        const pending = deferred<OperatorConversation[]>();
        api.getOperatorConversation.mockResolvedValueOnce({ conversation, frames: [] });
        api.listOperatorConversations.mockReturnValueOnce(pending.promise);
        await emit(metadataCall(1), metadataResult(2));
        api.listOperatorConversations.mockResolvedValueOnce([archivedRow]);
        await clickFilter("Archived");
        await act(async () => pending.resolve([conversation, other]));
        expect(container.querySelector("ul")?.textContent).toContain(archivedRow.title);
        expect(container.querySelector("ul")?.textContent).not.toContain(other.title);
      });

      it("does not resurrect a local archive when metadata refresh settles", async () => {
        const { conversation, other, emit } = await mountMetadataConversation();
        const pending = deferred<OperatorConversation[]>();
        api.getOperatorConversation.mockResolvedValueOnce({ conversation, frames: [] });
        api.listOperatorConversations.mockReturnValueOnce(pending.promise);
        await emit(metadataCall(1), metadataResult(2));
        api.updateOperatorConversation.mockResolvedValueOnce({
          ...conversation,
          status: "archived",
        });
        const archive = container.querySelector(
          'ul li button[aria-label="Archive"]',
        ) as HTMLButtonElement;
        await act(async () => archive.click());
        await act(async () => pending.resolve([conversation, other]));
        expect(container.querySelector("ul")?.textContent).not.toContain(conversation.title);
        expect(container.querySelector("ul")?.textContent).toContain(other.title);
      });

      async function holdMetadataRead() {
        const mounted = await mountMetadataConversation();
        const snapshot = deferred<OperatorConversationSnapshot>();
        const list = deferred<OperatorConversation[]>();
        api.getOperatorConversation.mockReturnValueOnce(snapshot.promise);
        api.listOperatorConversations.mockReturnValueOnce(list.promise);
        await mounted.emit(metadataCall(1), metadataResult(2));
        const settle = async (
          conversation: typeof mounted.conversation,
          rows: OperatorConversation[],
        ) => {
          await act(async () => {
            snapshot.resolve({ conversation, frames: [] });
            list.resolve(rows);
          });
        };
        return { ...mounted, settle };
      }

      async function renameRow(from: string, to: string) {
        const titleButton = [...container.querySelectorAll("ul li button")].find((button) =>
          button.querySelector("span")?.textContent?.endsWith(from),
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
          setNativeValue.call(input, to);
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
          await Promise.resolve();
        });
      }

      it("keeps a pin written while a metadata refresh is pending, placed as the server lists it", async () => {
        const { conversation, other, settle } = await holdMetadataRead();
        api.updateOperatorConversation.mockResolvedValueOnce({
          ...other,
          pinned: true,
          updatedAt: 30,
        });
        const pin = container.querySelector('ul li button[aria-label="Pin"]') as HTMLButtonElement;
        await act(async () => pin.click());
        await settle(conversation, [conversation, other]);
        const rows = [...container.querySelectorAll("ul li")];
        expect(rows.map((row) => row.querySelector("button span")?.textContent)).toEqual([
          "★ Other conversation",
          "★ Scheduler check",
        ]);
        expect(rows[0].querySelector('button[aria-label="Unpin"]')).not.toBeNull();
      });

      it("keeps a rename written while a metadata refresh is pending", async () => {
        const { conversation, other, settle } = await holdMetadataRead();
        api.updateOperatorConversation.mockResolvedValueOnce({
          ...conversation,
          title: "Renamed check",
        });
        await renameRow("Scheduler check", "Renamed check");
        expectMetadata("Renamed check", "Original scope");
        await settle(conversation, [conversation, other]);
        expectMetadata("Renamed check", "Original scope");
      });

      it("takes the description from a rename stamped later than a pending read", async () => {
        const { conversation, other, settle } = await holdMetadataRead();
        api.updateOperatorConversation.mockResolvedValueOnce({
          ...conversation,
          title: "Renamed check",
          description: "Newer scope",
          updatedAt: 30,
        });
        await renameRow("Scheduler check", "Renamed check");
        const older = { ...conversation, description: "Older scope", updatedAt: 20 };
        await settle(older, [older, other]);
        expectMetadata("Renamed check", "Newer scope");
      });

      it("takes a refresh that holds a later server change over a rename written during it", async () => {
        const { conversation, other, settle } = await holdMetadataRead();
        api.updateOperatorConversation.mockResolvedValueOnce({
          ...conversation,
          title: "Renamed check",
          updatedAt: 10,
        });
        await renameRow("Scheduler check", "Renamed check");
        const later = { ...conversation, title: "Server title", updatedAt: 20 };
        await settle(later, [later, other]);
        expectMetadata("Server title", "Original scope");
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

      // Starts two metadata reads and returns settle(index, ...), where index is the order
      // the reads started in: each read takes the next queued mock.
      async function startTwoMetadataReads() {
        const mounted = await mountMetadataConversation();
        const { conversation, other, emit } = mounted;
        const reads = [1, 2].map(() => ({
          snapshot: deferred<OperatorConversationSnapshot>(),
          list: deferred<OperatorConversation[]>(),
        }));
        for (const read of reads) {
          api.getOperatorConversation.mockReturnValueOnce(read.snapshot.promise);
          api.listOperatorConversations.mockReturnValueOnce(read.list.promise);
        }
        await emit(metadataCall(1), metadataResult(2));
        await emit(metadataCall(3, "request-2"), metadataResult(4, "request-2"));
        expect(api.getOperatorConversation).toHaveBeenCalledTimes(3);
        const settle = async (index: number, title: string, description: string) => {
          const updated = { ...conversation, title, description };
          await act(async () => {
            reads[index].snapshot.resolve({ conversation: updated, frames: [] });
            reads[index].list.resolve([updated, other]);
          });
        };
        return settle;
      }

      it("keeps a newer metadata read when an older one settles after it", async () => {
        const settle = await startTwoMetadataReads();
        await settle(1, "Newer title", "Newer scope");
        expectMetadata("Newer title", "Newer scope");
        await settle(0, "Older title", "Older scope");
        expectMetadata("Newer title", "Newer scope");
        expect(container.querySelector("ul")?.textContent).not.toContain("Older title");
      });

      it("applies metadata reads that settle in the order they started", async () => {
        const settle = await startTwoMetadataReads();
        await settle(0, "Older title", "Older scope");
        expectMetadata("Older title", "Older scope");
        await settle(1, "Newer title", "Newer scope");
        expectMetadata("Newer title", "Newer scope");
      });
    });

    it.each(["initial", "active-filter"])(
      "merges the %s list read with a conversation created while it was pending",
      async (source) => {
        const pending = deferred<OperatorConversation[]>();
        const existing: OperatorConversation = {
          id: "existing",
          title: "Earlier conversation",
          status: "active",
          pinned: false,
          activeRequestId: null,
        };
        const created: OperatorConversation = {
          id: "created",
          title: "Newly created conversation",
          status: "active",
          pinned: false,
          activeRequestId: null,
        };
        api.listOperatorConversations.mockReset();
        if (source === "initial")
          api.listOperatorConversations.mockReturnValueOnce(pending.promise);
        else api.listOperatorConversations.mockResolvedValueOnce([]);
        api.createOperatorConversation.mockResolvedValueOnce(created);
        api.submitOperatorTurn.mockResolvedValueOnce({ requestId: "created-turn" });
        await mount();
        if (source === "active-filter") {
          await openList();
          api.listOperatorConversations.mockReturnValueOnce(pending.promise);
          const active = [...container.querySelectorAll("button")].find(
            (button) => button.textContent === "Active",
          )!;
          await act(async () => active.click());
        }
        const input = container.querySelector("textarea")!;
        await act(async () => {
          Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")!.set!.call(
            input,
            "Inspect this project",
          );
          input.dispatchEvent(new Event("input", { bubbles: true }));
        });
        const send = [...container.querySelectorAll("button")].find(
          (button) => button.textContent === "Send",
        )!;
        await act(async () => send.click());
        expect(api.submitOperatorTurn).toHaveBeenCalledWith(created.id, expect.any(Object));
        await act(async () => pending.resolve([existing]));
        if (source === "initial") await openList();
        expect(container.querySelector("ul")?.textContent).toContain(existing.title);
        expect(container.querySelector("ul")?.textContent).toContain(created.title);
      },
    );

    it("drops a conversation load after a different conversation is selected", async () => {
      const first: OperatorConversation = {
        id: "conversation-1",
        title: "Scheduler check",
        status: "active",
        pinned: false,
        activeRequestId: null,
      };
      const other: OperatorConversation = {
        id: "conversation-2",
        title: "Other conversation",
        status: "active",
        pinned: false,
        activeRequestId: null,
      };
      api.listOperatorConversations.mockResolvedValueOnce([first, other]);
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: first, frames: [] });
      await mount();
      await openList();
      const pending = deferred<{ conversation: OperatorConversation; frames: OperatorFrame[] }>();
      api.getOperatorConversation.mockReturnValueOnce(pending.promise);
      const select = async (title: string) => {
        const button = [...container.querySelectorAll("ul li button")].find(
          (item) => item.textContent === title,
        ) as HTMLButtonElement;
        await act(async () => button.click());
      };
      await select("Other conversation");
      await openList();
      api.getOperatorConversation.mockResolvedValueOnce({ conversation: first, frames: [] });
      await select("Scheduler check");
      await act(async () => pending.resolve({ conversation: other, frames: [] }));
      expect(
        container.querySelector('button[aria-label^="Conversations"]')?.getAttribute("aria-label"),
      ).toBe("Conversations: Scheduler check");
      expect(window.localStorage.getItem("studio:operator-conversation")).toBe(first.id);
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
