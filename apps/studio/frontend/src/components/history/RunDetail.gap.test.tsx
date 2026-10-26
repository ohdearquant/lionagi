import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type { MessageStreamOptions, SessionDetail, SessionMessage, SessionSignalPage } from "@/lib/api";
import { SignalProjection } from "@/lib/signalProjection";

const api = vi.hoisted(() => ({
  getSession: vi.fn(), getSessionSignalPage: vi.fn(), streamSession: vi.fn(), streamSignals: vi.fn(),
}));
vi.mock("@/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api")>("@/lib/api")), ...api,
  getInvocation: vi.fn().mockRejectedValue(new Error("no resumed invocation")),
}));
vi.mock("@/components/history/ResumeRun", () => ({ default: () => null }));
vi.mock("@/components/canvas/WorkerCanvas", () => ({ default: () => null }));
vi.mock("@/components/RunStepCard", () => ({
  extractFilePaths: () => [],
  default: ({ step }: { step: { messages: Array<{ content: string }>; result: { message_count: number } } }) => (
    <><pre data-testid="ordered-messages">{JSON.stringify(step.messages.map((row) => row.content))}</pre>
      <span data-testid="message-total">{step.result.message_count}</span></>
  ),
}));
const { default: RunDetail } = await import("./RunDetail");

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { promise, resolve };
}
function row(index: number): SessionMessage {
  return { id: `m-${index}`, role: "user", content: { instruction: String(index) },
    sender: "operator", timestamp: index % 3 === 0 ? -index : 7, lion_class: "Instruction" };
}
function range(start: number, end: number) {
  return Array.from({ length: end - start + 1 }, (_, index) => row(start + index));
}
function detail(messages: SessionMessage[], total: number, cursor: string | null, ticket?: string): SessionDetail {
  return { id: "session", name: "session", created_at: 1, updated_at: 1, status: "running",
    branches: [{ id: "branch", name: "main", created_at: 1, messages, message_total: total }],
    message_next_cursor: cursor,
    ...(ticket ? { stream_snapshot: { version: 1, message_cursor: ticket, signal_seq: 0 } } : {}),
  };
}
let subscriptions: Array<{ event: (row: Record<string, unknown>) => void; options: MessageStreamOptions }>;
let root: Root;
let container: HTMLDivElement;
async function settle(action: () => void) {
  await act(async () => { action(); for (let index = 0; index < 12; index++) await Promise.resolve(); });
}
async function render() {
  await settle(() => root.render(<IntlProvider locale="en" messages={enMessages}><RunDetail id="session" /></IntlProvider>));
}
function loaded(): string[] {
  return JSON.parse(container.querySelector('[data-testid="ordered-messages"]')!.textContent!);
}
function expected(start: number, end: number) {
  return range(start, end).map((message) => String((message.content as { instruction: string }).instruction));
}
function olderButton() {
  return [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Load older messages"));
}
function resync(reason: string) {
  const sub = subscriptions.at(-1)!;
  sub.options.onResyncRequired!({ type: "resync_required", stream: "messages", reason },
    { cursor: sub.options.initialCursor });
}
beforeEach(() => {
  subscriptions = [];
  for (const mock of Object.values(api)) mock.mockReset();
  api.streamSession.mockImplementation((_id, event, _state, options) => {
    subscriptions.push({ event, options }); return vi.fn();
  });
  api.streamSignals.mockImplementation(() => vi.fn());
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  Element.prototype.scrollIntoView = vi.fn();
  window.history.replaceState(null, "", "/?view=list");
  container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); window.history.replaceState(null, "", "/"); vi.unstubAllGlobals(); });

describe("authoritative resnapshot history reachability", () => {
  it.each(["overflow", "expired", "foreign_epoch"])("%s recovers a multi-page gap in progression order and keeps a newer live suffix", async (reason) => {
    const firstPage = deferred<SessionDetail>();
    const initial = detail(range(1, 200), 200, null, "old-epoch-ticket");
    const fresh = detail(range(801, 1000), 1000, "before-801", "new-epoch-ticket");
    const pages: Record<string, SessionDetail> = {
      "before-601": detail(range(401, 600), 1000, "before-401"),
      "before-401": detail(range(201, 400), 1000, "before-201"),
      "before-201": detail(range(1, 200), 1000, null),
    };
    let bootstrapReads = 0;
    api.getSession.mockImplementation((_id: string, params?: { liveSnapshot?: boolean; messageCursor?: string }) => {
      if (params?.liveSnapshot) return Promise.resolve(bootstrapReads++ === 0 ? initial : fresh);
      if (params?.messageCursor === "before-801") return firstPage.promise;
      return Promise.resolve(pages[params!.messageCursor!]);
    });
    await render(); expect(loaded()).toEqual(expected(1, 200));
    await settle(() => resync(reason));
    expect(loaded()).toEqual(expected(801, 1000));
    await settle(() => olderButton()!.click());
    expect(api.getSession).toHaveBeenLastCalledWith("session", { messageCursor: "before-801" });
    await settle(() => subscriptions.at(-1)!.event({ ...row(1001), branch_id: "branch", timestamp: -999999 }));
    await settle(() => firstPage.resolve(detail(range(601, 800), 1000, "before-601")));
    expect(loaded()).toEqual(expected(601, 1001));
    expect(container.querySelector('[data-testid="message-total"]')!.textContent).toBe("1001");
    for (const lower of [401, 201, 1]) {
      await settle(() => olderButton()!.click());
      expect(loaded()).toEqual(expected(lower, 1001));
    }
    expect(new Set(loaded()).size).toBe(1001);
    expect(olderButton()).toBeUndefined();
    expect(api.getSession.mock.calls.filter(([, params]) => params?.messageCursor).map(([, params]) => params.messageCursor))
      .toEqual(["before-801", "before-601", "before-401", "before-201"]);
    expect(subscriptions.at(-1)!.options.initialCursor).toBe("new-epoch-ticket");
  });

  it.each(["expired", "foreign_epoch"])("%s cannot use matching count, tail IDs and anchor to retain an old-only progression member", async (reason) => {
    const tail = [...range(2, 200), { ...row(201), id: "anchor-z", content: { instruction: "z" } }];
    const oldOnly = { ...row(1), id: "old-a", content: { instruction: "old-a" } };
    const replacement = { ...row(1), id: "new-b", content: { instruction: "new-b" } };
    api.getSession.mockResolvedValueOnce(detail(tail, 201, "old-before-2", "old-ticket"))
      .mockResolvedValueOnce(detail([oldOnly], 201, null))
      .mockResolvedValueOnce(detail(tail, 201, "new-before-2", "new-ticket"))
      .mockResolvedValueOnce(detail([replacement], 201, null));
    await render(); await settle(() => olderButton()!.click());
    expect(loaded()).toContain("old-a");
    await settle(() => resync(reason));
    expect(loaded()).not.toContain("old-a"); expect(loaded()).toHaveLength(200);
    await settle(() => olderButton()!.click());
    expect(api.getSession).toHaveBeenLastCalledWith("session", { messageCursor: "new-before-2" });
    expect(loaded()).toEqual(["new-b", ...tail.map((message) => (message.content as { instruction: string }).instruction)]);
  });

  it("appends every bounded prefix page without publication, then publishes exactly once before subscribing", async () => {
    const last = deferred<SessionSignalPage>();
    const snapshot = vi.spyOn(SignalProjection.prototype, "snapshot");
    const append = vi.spyOn(SignalProjection.prototype, "append");
    try {
      api.getSession.mockResolvedValue({ ...detail([], 0, null, "ticket"),
        stream_snapshot: { version: 1, message_cursor: "ticket", signal_seq: 501 } });
      const signals = Array.from({ length: 501 }, (_, index) => ({ id: `sig-${index + 1}`,
        session_id: "session", seq: index + 1, kind: "SessionUpdated", op_id: "", ts: 1000, payload: {} }));
      api.getSessionSignalPage.mockResolvedValueOnce({ signals: signals.slice(0, 500),
        next_after_seq: 500, through_seq: 501, has_more: true }).mockReturnValueOnce(last.promise);
      await render();
      expect(append).toHaveBeenCalledTimes(500); expect(snapshot).not.toHaveBeenCalled();
      expect(api.streamSession).not.toHaveBeenCalled(); expect(api.streamSignals).not.toHaveBeenCalled();
      await settle(() => last.resolve({ signals: signals.slice(500), next_after_seq: 501,
        through_seq: 501, has_more: false }));
      expect(append).toHaveBeenCalledTimes(501); expect(snapshot).toHaveBeenCalledTimes(1);
      expect(api.streamSession).toHaveBeenCalledTimes(1); expect(api.streamSignals).toHaveBeenCalledTimes(1);
    } finally { snapshot.mockRestore(); append.mockRestore(); }
  });
});
