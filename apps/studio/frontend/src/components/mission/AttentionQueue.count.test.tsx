/**
 * AttentionQueue — the heading count and the page summary agree.
 *
 * Mission Control shows two attention numbers a line apart: the page summary
 * ("N need attention") and this section's heading chip. They used to disagree,
 * because the summary counted items still awaiting a disposition while the
 * chip counted every active item, acknowledged ones included. Both were
 * correct by their own definition and both were labelled attention, so the
 * page contradicted itself on its own landing screen.
 *
 * The chip now renders a count passed in from the same value the summary
 * uses. Items that have been answered stay visible as rows — they are still
 * there to undo — but they no longer inflate a heading that says "needs
 * attention".
 *
 * The total remains derivable: reasons outside the actionable set render as
 * digest rows carrying their own per-reason count.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { AttentionItem } from "./boardReducer";

// The rows are router links, and a bare render has no router context. Only
// the counting is under test here, so the link becomes a plain anchor.
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

function item(id: string, acknowledged: boolean): AttentionItem {
  return {
    id,
    kind: "run",
    name: `run ${id}`,
    reason: "gated",
    startedAt: 1000,
    href: `/fleet?s=${id}`,
    status: "running",
    ...(acknowledged ? { disposition: { state: "acknowledged" } as never } : {}),
  };
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  root = null;
  container = null;
});

function render(node: React.ReactElement) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root!.render(
      <IntlProvider locale="en" messages={enMessages}>
        {node}
      </IntlProvider>,
    );
  });
  return container;
}

describe("AttentionQueue — heading count", () => {
  it("counts items awaiting a disposition, not every active item", () => {
    // Eight active items, five of them already answered. The old chip read 8.
    const items = [
      item("a", false),
      item("b", false),
      item("c", false),
      item("d", true),
      item("e", true),
      item("f", true),
      item("g", true),
      item("h", true),
    ];
    const el = render(
      <AttentionQueue
        items={items}
        dischargedItems={[]}
        unacknowledgedCount={3}
        nowSec={2000}
        dataState="live"
      />,
    );

    const heading = el.querySelector('[aria-labelledby="attention-heading"]');
    expect(heading, "the section did not render").not.toBeNull();
    // Scope to the heading row: item rows carry ids and ages that could
    // otherwise supply a stray "3" or "8" and make this pass by accident.
    const headingRow = heading!.firstElementChild;
    const chipText = (headingRow?.textContent ?? "").replace(/\s+/g, " ");

    expect(chipText).toContain("3");
    expect(chipText, "the heading still reflects every active item").not.toContain("8");
  });

  it("keeps answered items visible as rows even though they are not counted", () => {
    const items = [item("a", false), item("b", true), item("c", true)];
    const el = render(
      <AttentionQueue
        items={items}
        dischargedItems={[]}
        unacknowledgedCount={1}
        nowSec={2000}
        dataState="live"
      />,
    );
    const text = el.textContent ?? "";
    // All three are still on the page — the count changed, not the list.
    expect(text).toContain("run a");
    expect(text).toContain("run b");
    expect(text).toContain("run c");
  });
});
