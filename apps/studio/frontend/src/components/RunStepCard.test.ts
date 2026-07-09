import { describe, it, expect } from "vitest";
import {
  runMessageMemoKey,
  runMessagesEqualForMemo,
  stepPropsEqual,
  collapsedTextFor,
} from "./RunStepCard";
import type { RunMessage, RunStep } from "@/lib/types";

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
