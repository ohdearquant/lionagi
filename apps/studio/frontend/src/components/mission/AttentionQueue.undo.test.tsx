/**
 * Undo clears every key the disposition could be stored under.
 *
 * The reducer joins a disposition found under an item's pre-session-identity
 * key onto its current id, so an undo that clears only the current key leaves
 * the legacy row behind and the next poll re-joins it: the row re-discharges
 * itself and Undo reads as a no-op.
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { AttentionItem } from "./boardReducer";

const api = vi.hoisted(() => ({
  putAttentionDisposition: vi.fn(),
  deleteAttentionDisposition: vi.fn(),
  ApiError: class ApiError extends Error {},
}));

vi.mock("@/lib/api", () => api);

vi.mock("@tanstack/react-router", () => ({
  Link: ({
    to,
    children,
    ...rest
  }: {
    to?: string;
    children?: React.ReactNode;
  } & React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={typeof to === "string" ? to : undefined} {...rest}>
      {children}
    </a>
  ),
}));

const { default: AttentionQueue } = await import("./AttentionQueue");

function item(overrides: Partial<AttentionItem> = {}): AttentionItem {
  return {
    id: "run:current",
    kind: "run",
    name: "run current",
    reason: "stuck",
    startedAt: 1000,
    href: "/fleet?s=run:current",
    status: "running",
    disposition: { state: "acknowledged" } as AttentionItem["disposition"],
    ...overrides,
  };
}

describe("AttentionQueue — undo", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    api.putAttentionDisposition.mockResolvedValue(undefined);
    api.deleteAttentionDisposition.mockReset();
    api.deleteAttentionDisposition.mockResolvedValue(undefined);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  function renderQueue(items: AttentionItem[]) {
    act(() => {
      root.render(
        <IntlProvider locale="en" messages={enMessages}>
          <AttentionQueue
            items={items}
            dischargedItems={[]}
            unacknowledgedCount={0}
            nowSec={2000}
            dataState="live"
          />
        </IntlProvider>,
      );
    });
  }

  async function clickUndo() {
    const undo = [...container.querySelectorAll("button")].find(
      (b) => b.textContent?.trim() === "Undo",
    );
    expect(undo, "no Undo button rendered").toBeDefined();
    await act(async () => {
      undo!.click();
      await Promise.resolve();
    });
  }

  it("clears only the current key when there is no legacy id", async () => {
    renderQueue([item()]);
    await clickUndo();

    expect(api.deleteAttentionDisposition.mock.calls).toEqual([["run:current"]]);
  });

  it("clears the older key as well when the row carries one", async () => {
    renderQueue([item({ legacyId: "run:legacy" })]);
    await clickUndo();

    expect(api.deleteAttentionDisposition.mock.calls).toEqual([["run:current"], ["run:legacy"]]);
  });
});
