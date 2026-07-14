import { act } from "react";
import * as React from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import { afterEach, describe, it, expect, vi } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import enMessages from "@/messages/en.json";

vi.mock("@/components/ui/Markdown", async () => {
  const ReactModule = await import("react");
  return {
    default: ({ children }: { children?: React.ReactNode }) =>
      ReactModule.createElement("div", null, children),
  };
});

import {
  default as RunStepCard,
  runMessageMemoKey,
  runMessagesEqualForMemo,
  stepPropsEqual,
  collapsedTextFor,
  extractFilePaths,
  pathFromArgs,
} from "./RunStepCard";
import type { RunMessage, RunStep } from "@/lib/types";

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

function toolMessage(overrides: Partial<RunMessage> = {}): RunMessage {
  return {
    role: "tool_call",
    function: "Bash",
    summary: "ls",
    output: "a.txt",
    status: "ok",
    exit_code: 0,
    timestamp: 1,
    ...overrides,
  };
}

function step(overrides: Partial<RunStep> = {}, messages: RunMessage[] = []): RunStep {
  return {
    step: "s1",
    status: "running",
    timestamp: 1,
    messages,
    ...overrides,
  };
}

function props(step_: RunStep): Parameters<typeof stepPropsEqual>[0] {
  return { step: step_, defaultExpanded: false, expanded: undefined, onToggleExpand: undefined };
}

describe("runMessageMemoKey", () => {
  it("produces the same key for identical message content", () => {
    const a = toolMessage();
    const b = toolMessage();
    expect(runMessageMemoKey(a)).toBe(runMessageMemoKey(b));
  });

  it("changes when tool-call output changes", () => {
    const a = toolMessage({ output: "a.txt" });
    const b = toolMessage({ output: "a.txt\nb.txt" });
    expect(runMessageMemoKey(a)).not.toBe(runMessageMemoKey(b));
  });

  it("changes when tool-call status changes", () => {
    const a = toolMessage({ status: "ok" });
    const b = toolMessage({ status: "error" });
    expect(runMessageMemoKey(a)).not.toBe(runMessageMemoKey(b));
  });

  it("changes when exit_code changes", () => {
    const a = toolMessage({ exit_code: 0 });
    const b = toolMessage({ exit_code: 1 });
    expect(runMessageMemoKey(a)).not.toBe(runMessageMemoKey(b));
  });

  it("changes when arguments change", () => {
    const a = toolMessage({ arguments: { path: "a" } });
    const b = toolMessage({ arguments: { path: "b" } });
    expect(runMessageMemoKey(a)).not.toBe(runMessageMemoKey(b));
  });

  it("changes when assistant content changes", () => {
    const a: RunMessage = { role: "assistant", content: "hello" };
    const b: RunMessage = { role: "assistant", content: "goodbye" };
    expect(runMessageMemoKey(a)).not.toBe(runMessageMemoKey(b));
  });

  it("is unaffected by a sender-only change (sender is not rendered)", () => {
    const a = toolMessage({ sender: "agent-a" });
    const b = toolMessage({ sender: "agent-b" });
    expect(runMessageMemoKey(a)).toBe(runMessageMemoKey(b));
  });
});

describe("runMessagesEqualForMemo", () => {
  it("returns true for equal message arrays", () => {
    expect(runMessagesEqualForMemo([toolMessage()], [toolMessage()])).toBe(true);
  });

  it("returns false when the same message count has a changed final tool output", () => {
    const prev = [toolMessage({ output: "old" })];
    const next = [toolMessage({ output: "new" })];
    expect(runMessagesEqualForMemo(prev, next)).toBe(false);
  });

  it("returns false when message counts differ", () => {
    expect(runMessagesEqualForMemo([toolMessage()], [toolMessage(), toolMessage()])).toBe(false);
  });

  it("treats undefined as an empty array", () => {
    expect(runMessagesEqualForMemo(undefined, [])).toBe(true);
  });
});

describe("collapsedTextFor (ToolCallBlock output-preview fallback)", () => {
  it("prefers summary when present, even if output is also present", () => {
    expect(collapsedTextFor("ls -la", "a.txt\nb.txt")).toBe("ls -la");
  });

  it("falls back to the first non-blank output line when summary is empty", () => {
    expect(collapsedTextFor("", "line one\nline two")).toBe("line one");
  });

  it("skips leading blank/whitespace-only lines to find the first real content", () => {
    expect(collapsedTextFor("", "\n   \n\nreal content\nmore")).toBe("real content");
  });

  it("returns empty string when output is entirely whitespace", () => {
    expect(collapsedTextFor("", "\n   \n\t\n")).toBe("");
  });

  it("returns empty string when output is undefined/empty and summary is empty", () => {
    expect(collapsedTextFor("", "")).toBe("");
  });

  it("trims surrounding whitespace from the selected output line", () => {
    expect(collapsedTextFor("", "   padded line   \nnext")).toBe("padded line");
  });
});

describe("stepPropsEqual", () => {
  it("returns true when props, step metadata, and messages are all unchanged", () => {
    const prev = props(step({}, [toolMessage()]));
    const next = props(step({}, [toolMessage()]));
    expect(stepPropsEqual(prev, next)).toBe(true);
  });

  it("returns false when a tool-call output changes but message count stays the same", () => {
    const prev = props(step({}, [toolMessage({ output: "old" })]));
    const next = props(step({}, [toolMessage({ output: "new" })]));
    expect(stepPropsEqual(prev, next)).toBe(false);
  });

  it("returns false when a tool-call status changes", () => {
    const prev = props(step({}, [toolMessage({ status: "ok" })]));
    const next = props(step({}, [toolMessage({ status: "error" })]));
    expect(stepPropsEqual(prev, next)).toBe(false);
  });

  it("returns false when result.agent differs", () => {
    const prev = props(step({ result: { agent: "a" } }));
    const next = props(step({ result: { agent: "b" } }));
    expect(stepPropsEqual(prev, next)).toBe(false);
  });

  it("returns false when result.model differs", () => {
    const prev = props(step({ result: { model: "sonnet" } }));
    const next = props(step({ result: { model: "opus" } }));
    expect(stepPropsEqual(prev, next)).toBe(false);
  });

  it("returns true when only sender changes on an otherwise-identical message", () => {
    const prev = props(step({}, [toolMessage({ sender: "agent-a" })]));
    const next = props(step({}, [toolMessage({ sender: "agent-b" })]));
    expect(stepPropsEqual(prev, next)).toBe(true);
  });
});

describe("extractFilePaths — the known file surface behind file links", () => {
  it("collects file_path/path args from tool_call and action messages", () => {
    const messages: RunMessage[] = [
      toolMessage({ function: "Write", arguments: { file_path: "/runs/r1/a/notes.md" } }),
      toolMessage({
        role: "action",
        function: "Edit",
        arguments: { path: "/runs/r1/a/review.md" },
      }),
    ];
    expect(extractFilePaths(messages).sort()).toEqual(
      ["/runs/r1/a/notes.md", "/runs/r1/a/review.md"].sort(),
    );
  });

  it("dedupes repeated paths across multiple tool calls", () => {
    const messages: RunMessage[] = [
      toolMessage({ function: "Read", arguments: { file_path: "/runs/r1/a/notes.md" } }),
      toolMessage({ function: "Edit", arguments: { file_path: "/runs/r1/a/notes.md" } }),
    ];
    expect(extractFilePaths(messages)).toEqual(["/runs/r1/a/notes.md"]);
  });

  it("ignores non-tool messages (assistant/user/system)", () => {
    const messages: RunMessage[] = [
      { role: "assistant", content: "Wrote notes.md" },
      { role: "user", content: "please write notes.md" },
    ];
    expect(extractFilePaths(messages)).toEqual([]);
  });

  it("returns an empty list when there is no file activity", () => {
    expect(extractFilePaths([])).toEqual([]);
  });
});

describe("RunStepCard overview message browser", () => {
  let container: HTMLDivElement | null = null;
  let root: Root | null = null;

  afterEach(() => {
    if (root) {
      act(() => root?.unmount());
    }
    container?.remove();
    container = null;
    root = null;
  });

  async function mount(messages: RunMessage[], extraProps: Record<string, unknown> = {}) {
    container = document.createElement("div");
    document.body.appendChild(container);
    await act(async () => {
      root = createRoot(container!);
      root.render(
        React.createElement(
          IntlProvider,
          { locale: "en", messages: enMessages } as unknown as React.ComponentProps<
            typeof IntlProvider
          >,
          React.createElement(RunStepCard, {
            step: step({}, messages),
            defaultExpanded: true,
            ...extraProps,
          }),
        ),
      );
      await Promise.resolve();
    });
  }

  async function click(button: HTMLButtonElement) {
    await act(async () => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
  }

  it("shows the complete latest message in a bounded scroll container", async () => {
    const longContent = `${"a".repeat(1300)}-complete-tail`;
    await mount([{ role: "assistant", content: longContent }]);

    const body = container?.querySelector<HTMLElement>("[data-overview-message-body]");
    expect(body).not.toBeNull();
    expect(body?.className).toContain("max-h-");
    expect(body?.className).toContain("overflow-y-auto");
    expect(body?.textContent).toContain("-complete-tail");
  });

  it("moves backward and forward over every step message", async () => {
    await mount([
      { role: "user", content: "first prompt" },
      { role: "assistant", content: "middle response" },
      { role: "assistant", content: "latest response" },
    ]);

    expect(container?.textContent).toContain("latest response");
    const previous = container?.querySelector<HTMLButtonElement>(
      'button[aria-label="Previous message"]',
    );
    expect(previous).not.toBeNull();
    await click(previous!);
    expect(container?.textContent).toContain("middle response");

    const next = container?.querySelector<HTMLButtonElement>('button[aria-label="Next message"]');
    expect(next).not.toBeNull();
    await click(next!);
    expect(container?.textContent).toContain("latest response");
  });

  it("requests an older page when previous is used at the first loaded message", async () => {
    const onLoadOlder = vi.fn();
    await mount(
      [
        { role: "user", content: "oldest loaded" },
        { role: "assistant", content: "latest loaded" },
      ],
      { olderMessagesRemaining: 4, onLoadOlder },
    );

    const previous = container?.querySelector<HTMLButtonElement>(
      'button[aria-label="Previous message"]',
    );
    await click(previous!);
    await click(previous!);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });
});

describe("RunDetail older-message wiring", () => {
  const src = fs.readFileSync(path.resolve(__dirname, "history/RunDetail.tsx"), "utf-8");

  it("passes its existing older-page handler and state into branch cards", () => {
    expect(src).toMatch(/onLoadOlder=\{handleLoadOlder\}/);
    expect(src).toMatch(/olderMessagesRemaining=\{hiddenOlderCount\}/);
    expect(src).toMatch(/loadingOlder=\{loadingOlder\}/);
  });
});

describe("pathFromArgs — shell-derived file paths", () => {
  it("strips shell separators and dedupes the normalized file path", () => {
    expect(
      pathFromArgs(
        { command: "python /repo/scripts/check.py; sed -n '1p' /repo/scripts/check.py" },
        "",
      ),
    ).toEqual(["/repo/scripts/check.py"]);
  });

  it("excludes root markers, directory-looking paths, and interpreter binaries", () => {
    expect(
      pathFromArgs(
        { command: "cd //; cd /repo/worktree; /repo/.venv/bin/li run /repo/src/main.py" },
        "",
      ),
    ).toEqual(["/repo/src/main.py"]);
  });

  it("trusts structured file_path values even when a filename has no extension", () => {
    expect(pathFromArgs({ file_path: "/repo/Makefile" }, "", "Read")).toEqual(["/repo/Makefile"]);
  });

  it("does not treat a directory-valued path from a search tool as a file", () => {
    expect(pathFromArgs({ path: "/repo/src" }, "", "Glob")).toEqual([]);
  });

  it.each([
    ["cat src/worker.py", ["src/worker.py"]],
    ["cat ./README", ["README"]],
    ["cat ./.env", [".env"]],
    ["cat bin/config.json", ["bin/config.json"]],
  ])("retains ordinary Bash file operand from %s", (command, expected) => {
    expect(pathFromArgs({ command }, "", "Bash")).toEqual(expected);
  });

  it("normalizes lexical aliases before deduplication", () => {
    expect(
      pathFromArgs({ command: "cat /repo/src/a.py /repo/src/../src/a.py" }, "", "Bash"),
    ).toEqual(["/repo/src/a.py"]);
  });
});
