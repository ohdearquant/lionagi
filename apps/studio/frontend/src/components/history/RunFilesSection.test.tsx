import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import RunFilesSection from "@/components/history/RunFilesSection";
import type { RunFileSummary } from "@/lib/types";

const mounted: Array<{ container: HTMLDivElement; root: Root }> = [];

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
});

afterEach(() => {
  for (const { container, root } of mounted) {
    act(() => root.unmount());
    container.remove();
  }
  mounted.length = 0;
});

function renderSummary(summary: RunFileSummary) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  mounted.push({ container, root });
  act(() => {
    root.render(
      <IntlProvider locale="en" messages={enMessages}>
        <RunFilesSection runId="run-1" summary={summary} />
      </IntlProvider>,
    );
  });
  return container;
}

function clickButton(container: HTMLElement, label: string) {
  const button = Array.from(container.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === label,
  );
  expect(button, `button '${label}'`).toBeTruthy();
  act(() => button!.click());
}

describe("RunFilesSection", () => {
  it("renders server-owned mixed access without inventing a mode", () => {
    const container = renderSummary({
      items: [
        { path: "src/app.py", access: ["read", "write"], openable: true },
        { path: "docs/reference.md", access: ["read"], openable: true },
        { path: "reports/result.json", access: ["write"], openable: true },
        { path: "future/tool.dat", access: [], openable: false },
      ],
      total: 4,
      shown: 4,
      truncated: false,
      redacted_count: 0,
    });

    expect(container.querySelectorAll('[data-testid="run-file-item"]')).toHaveLength(4);
    expect(container.querySelectorAll('[data-access="read"]')).toHaveLength(2);
    expect(container.querySelectorAll('[data-access="write"]')).toHaveLength(2);
    expect(container.textContent).toContain("future/tool.dat");
    expect(container.querySelector('[data-path="future/tool.dat"]')?.tagName).toBe("SPAN");
  });

  it("filters read and written files through visible buttons", () => {
    const container = renderSummary({
      items: [
        { path: "both.py", access: ["read", "write"], openable: true },
        { path: "read.py", access: ["read"], openable: true },
        { path: "write.py", access: ["write"], openable: true },
        { path: "unknown.py", access: [], openable: true },
      ],
      total: 4,
      shown: 4,
      truncated: false,
      redacted_count: 0,
    });

    clickButton(container, "Written");
    expect(container.querySelectorAll('[data-testid="run-file-item"]')).toHaveLength(2);
    expect(container.textContent).toContain("both.py");
    expect(container.textContent).toContain("write.py");
    expect(container.textContent).not.toContain("read.py");

    clickButton(container, "Read");
    expect(container.querySelectorAll('[data-testid="run-file-item"]')).toHaveLength(2);
    expect(container.textContent).toContain("read.py");
    expect(container.textContent).not.toContain("write.py");
  });

  it("reveals a large server-bounded window in small chunks", () => {
    const items: RunFileSummary["items"] = Array.from({ length: 100 }, (_, index) => ({
      path: `src/file-${String(index).padStart(3, "0")}.py`,
      access: ["read"],
      openable: true,
    }));
    const container = renderSummary({
      items,
      total: 2_500,
      shown: 100,
      truncated: true,
      redacted_count: 3,
    });

    expect(container.querySelectorAll('[data-testid="run-file-item"]')).toHaveLength(20);
    expect(container.textContent).toContain("20 of 2,500");
    expect(container.textContent).toContain("3 unsafe paths hidden");

    clickButton(container, "Show more");
    expect(container.querySelectorAll('[data-testid="run-file-item"]')).toHaveLength(40);
    expect(container.textContent).toContain("40 of 2,500");
  });
});
