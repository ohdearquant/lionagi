import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type {
  OperatorConversationSnapshot,
  OperatorFrame,
  OperatorModelCatalogEntry,
} from "@/lib/types";

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
  streamOperatorConversation: vi.fn(() => vi.fn()),
  submitOperatorTurn: vi.fn(),
  updateOperatorConversation: vi.fn(),
  getRunFile: vi.fn(),
}));
const router = vi.hoisted(() => ({ navigate: vi.fn() }));

vi.mock("@/lib/api", () => api);
vi.mock("@tanstack/react-router", () => ({
  Link: ({ children }: { children?: React.ReactNode }) => <a href="/fleet">{children}</a>,
  useLocation: () => ({ pathname: "/", search: {} }),
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

  it("ignores a slower conversation response after the operator selects a newer one", async () => {
    let resolveSlow!: (snapshot: OperatorConversationSnapshot) => void;
    const slowSnapshot = new Promise<OperatorConversationSnapshot>((resolve) => {
      resolveSlow = resolve;
    });
    const snapshot = (
      id: string,
      title: string,
      transcript: string,
    ): OperatorConversationSnapshot => ({
      conversation: { id, title, status: "active", activeRequestId: null, pinned: false },
      frames: [
        {
          ...textFrame(1, "assistant", transcript),
          conversationId: id,
        },
      ],
    });
    api.listOperatorConversations.mockResolvedValue([
      {
        id: "conversation-slow",
        title: "Slow conversation",
        status: "active",
        activeRequestId: null,
        updatedAt: 20,
      },
      {
        id: "conversation-fast",
        title: "Fast conversation",
        status: "active",
        activeRequestId: null,
        updatedAt: 10,
      },
    ]);
    api.getOperatorConversation.mockImplementation((id: string) =>
      id === "conversation-slow"
        ? slowSnapshot
        : Promise.resolve(snapshot(id, "Fast conversation", "Fast transcript wins")),
    );

    await mount();
    const toggle = container.querySelector(
      'button[aria-label^="Conversations"]',
    ) as HTMLButtonElement;
    await act(async () => toggle.click());
    const fastRow = [...container.querySelectorAll("ul li button")].find((button) =>
      button.textContent?.includes("Fast conversation"),
    ) as HTMLButtonElement;
    await act(async () => {
      fastRow.click();
      await Promise.resolve();
    });
    expect(container.textContent).toContain("Fast transcript wins");

    await act(async () => {
      resolveSlow(snapshot("conversation-slow", "Slow conversation", "Stale slow transcript"));
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Fast transcript wins");
    expect(container.textContent).not.toContain("Stale slow transcript");
    expect(window.localStorage.getItem("studio:operator-conversation")).toBe("conversation-fast");
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
