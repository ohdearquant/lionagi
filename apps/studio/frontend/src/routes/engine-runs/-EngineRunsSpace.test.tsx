import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { EngineRunPage, EngineRunSummary } from "@/lib/api";

const api = vi.hoisted(() => ({
  listEngineRuns: vi.fn(),
  getEngineRun: vi.fn(),
}));
const router = vi.hoisted(() => ({
  search: {} as { kind?: string; status?: string; session_id?: string; s?: string },
  navigate: vi.fn(() => Promise.resolve()),
}));

vi.mock("@/lib/api", () => api);
vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => router.navigate,
}));

const { default: EngineRunsSpace } = await import("@/components/engine-runs/EngineRunsSpace");

function run(index: number): EngineRunSummary {
  return {
    id: `run-${index}`,
    kind: `engine-${index}`,
    status: "completed",
    started_at: index,
    ended_at: index + 1,
    session_id: null,
    invocation_id: null,
    signal_session_id: null,
    parent_session_id: null,
    outcome: null,
    has_output: false,
    error_code: null,
  };
}

function page(items: EngineRunSummary[], nextCursor: string | null = null): EngineRunPage {
  return { version: 1, items, next_cursor: nextCursor };
}

describe("EngineRunsSpace", () => {
  let container: HTMLDivElement;
  let root: Root | null;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    router.search = {};
    router.navigate.mockClear();
    api.listEngineRuns.mockReset();
    api.getEngineRun.mockReset();
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
          <EngineRunsSpace search={router.search} />
        </IntlProvider>,
      );
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("keeps filter edits local until the user submits Apply", async () => {
    api.listEngineRuns.mockResolvedValue(page([]));
    await mount();
    expect(api.listEngineRuns).toHaveBeenCalledOnce();

    const kind = container.querySelector<HTMLInputElement>('input[aria-label="Filter by kind"]');
    await act(async () => {
      if (kind) {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(
          kind,
          "chat",
        );
        kind.dispatchEvent(new Event("input", { bubbles: true }));
      }
      await Promise.resolve();
    });
    expect(api.listEngineRuns).toHaveBeenCalledOnce();

    await act(async () => {
      container
        .querySelector("form")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(api.listEngineRuns).toHaveBeenCalledTimes(2);
    expect(api.listEngineRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({ kind: "chat", limit: 100 }),
    );
  });

  it("loads the next page so rows beyond the first 100 are reachable", async () => {
    api.listEngineRuns
      .mockResolvedValueOnce(
        page(
          Array.from({ length: 100 }, (_, index) => run(index)),
          "c1",
        ),
      )
      .mockResolvedValueOnce(page([run(100)]));
    await mount();

    const loadMore = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Load more",
    );
    expect(loadMore).toBeDefined();

    await act(async () => {
      loadMore?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(api.listEngineRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({ limit: 100, cursor: "c1" }),
    );
    expect(container.textContent).toContain("engine-100");
  });

  it("cancels stale load-more state when filters start a new first page", async () => {
    let resolveOldPage!: (runs: EngineRunPage) => void;
    let resolveNewPage!: (runs: EngineRunPage) => void;
    api.listEngineRuns.mockImplementation((options: { kind?: string; cursor?: string } = {}) => {
      if (options.kind === "chat" && options.cursor === "c-chat") {
        return new Promise<EngineRunPage>((resolve) => {
          resolveNewPage = resolve;
        });
      }
      if (options.kind === "chat") {
        return Promise.resolve(
          page(
            Array.from({ length: 100 }, (_, index) => run(index + 200)),
            "c-chat",
          ),
        );
      }
      if (options.cursor === "c-all") {
        return new Promise<EngineRunPage>((resolve) => {
          resolveOldPage = resolve;
        });
      }
      return Promise.resolve(
        page(
          Array.from({ length: 100 }, (_, index) => run(index)),
          "c-all",
        ),
      );
    });
    await mount();

    const buttonNamed = (name: string) =>
      Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find(
        (button) => button.textContent === name,
      );
    await act(async () => {
      buttonNamed("Load more")?.click();
      await Promise.resolve();
    });
    expect(buttonNamed("Loading…")?.disabled).toBe(true);

    const kind = container.querySelector<HTMLInputElement>('input[aria-label="Filter by kind"]');
    await act(async () => {
      if (kind) {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(
          kind,
          "chat",
        );
        kind.dispatchEvent(new Event("input", { bubbles: true }));
      }
      await Promise.resolve();
    });
    await act(async () => {
      container
        .querySelector("form")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(buttonNamed("Load more")?.disabled).toBe(false);
    await act(async () => {
      buttonNamed("Load more")?.click();
      await Promise.resolve();
    });
    expect(buttonNamed("Loading…")?.disabled).toBe(true);

    await act(async () => {
      resolveOldPage(page([run(100)]));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(buttonNamed("Loading…")?.disabled).toBe(true);
    expect(container.textContent).not.toContain("engine-100");

    await act(async () => {
      resolveNewPage(page([run(300)]));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(container.textContent).toContain("engine-300");
  });
});
