import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IntlProvider } from "use-intl";
import enMessages from "@/messages/en.json";
import type {
  MessageStreamOptions, SessionDetail, SessionSignalPage, SignalEvent, SignalStreamOptions,
} from "@/lib/api";

const canvas = vi.hoisted(() => vi.fn());
const api = vi.hoisted(() => ({
  getSession: vi.fn(), getSessionSignalPage: vi.fn(), streamSession: vi.fn(), streamSignals: vi.fn(),
}));
vi.mock("@/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api")>("@/lib/api")), ...api,
  getInvocation: vi.fn().mockRejectedValue(new Error("no resumed invocation")),
}));
vi.mock("@/components/history/ResumeRun", () => ({ default: () => null }));
vi.mock("@/components/ui/Markdown", () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("@/components/canvas/WorkerCanvas", () => ({ default: (props: unknown) => { canvas(props); return null; } }));
const { default: RunDetail } = await import("./RunDetail");

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function message(id: string) {
  return { id, role: "user", content: { instruction: id }, sender: "operator", timestamp: 1, lion_class: "Instruction" };
}
function session(id = "s1", ticket: string | null = "ticket-one", cut = 0): SessionDetail {
  return {
    id, name: `name-${id}`, created_at: 1, updated_at: 1, status: "running",
    branches: [{ id: "b1", name: "main", created_at: 1, message_total: 1, messages: [message("initial-message")] }],
    ...(ticket === null ? {} : { stream_snapshot: { version: 1, message_cursor: ticket, signal_seq: cut } }),
  } as SessionDetail;
}
function page(seq: number, id = "s1"): SessionSignalPage {
  return { signals: [{ id: `sig-${seq}`, session_id: id, seq, kind: "NodeStarted", op_id: "op", ts: 1_000, payload: { name: "worker" } }], next_after_seq: seq, through_seq: seq, has_more: false };
}
interface MessageSubscription {
  id: string; event: (value: Record<string, unknown>) => void; options?: MessageStreamOptions;
  stop: ReturnType<typeof vi.fn>;
}
interface SignalSubscription {
  id: string; event: (value: SignalEvent | { type: string }) => void; options?: SignalStreamOptions;
  stop: ReturnType<typeof vi.fn>;
}
let messages: MessageSubscription[];
let signals: SignalSubscription[];
let container: HTMLDivElement;
let root: Root;
async function render(id = "s1") {
  await act(async () => {
    root.render(<IntlProvider locale="en" messages={enMessages}><RunDetail id={id} /></IntlProvider>);
    for (let step = 0; step < 12; step++) await Promise.resolve();
  });
}
async function settle(action: () => void) {
  await act(async () => { action(); for (let step = 0; step < 12; step++) await Promise.resolve(); });
}
function overflow(subscription = messages.at(-1)!) {
  subscription.options!.onResyncRequired!(
    { type: "resync_required", stream: "messages", reason: "overflow", resume_cursor: "newest-enqueued" },
    { cursor: subscription.options!.initialCursor },
  );
}
function olderButton() {
  return [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Load older messages"));
}
beforeEach(() => {
  messages = []; signals = []; canvas.mockReset();
  for (const mock of Object.values(api)) mock.mockReset();
  api.streamSession.mockImplementation((id, event, _state, options) => {
    const stop = vi.fn(); messages.push({ id, event, options, stop }); return stop;
  });
  api.streamSignals.mockImplementation((id, event, _state, options) => {
    const stop = vi.fn(); signals.push({ id, event, options, stop }); return stop;
  });
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  Element.prototype.scrollIntoView = vi.fn();
  window.history.replaceState(null, "", "/?view=list");
  container = document.createElement("div"); document.body.appendChild(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); window.history.replaceState(null, "", "/"); vi.unstubAllGlobals(); });

describe("RunDetail paired live handoff", () => {
  it("waits for both detail and the bounded signal prefix before subscribing, then accepts the intervening message once", async () => {
    const detail = deferred<SessionDetail>(); const prefix = deferred<SessionSignalPage>();
    api.getSession.mockReturnValueOnce(detail.promise); api.getSessionSignalPage.mockReturnValueOnce(prefix.promise);
    await render();
    expect(messages).toHaveLength(0); expect(signals).toHaveLength(0);
    await settle(() => detail.resolve(session("s1", "paired-ticket", 2)));
    expect(messages).toHaveLength(0); expect(signals).toHaveLength(0);
    expect(api.getSession).toHaveBeenCalledWith("s1", { liveSnapshot: true });
    expect(api.getSessionSignalPage).toHaveBeenCalledWith("s1", { afterSeq: 0, throughSeq: 2, limit: 500 });
    await settle(() => prefix.resolve(page(2)));
    expect(messages).toHaveLength(1); expect(signals).toHaveLength(1);
    expect(messages[0]!.options).toMatchObject({ mode: "live", initialCursor: "paired-ticket" });
    expect(signals[0]!.options).toMatchObject({ mode: "live", initialSnapshot: "paired-ticket", initialAfterSeq: 2 });
    expect(() => messages[0]!.event({ id: "not-a-message", branch_id: "b1" })).toThrow("Invalid live message frame");
    const fresh = { ...message("between-get-and-subscribe"), branch_id: "b1" };
    await settle(() => { messages[0]!.event(fresh); messages[0]!.event(fresh); });
    expect(container.textContent).toContain("between-get-and-subscribe");
    expect(container.textContent).toContain("/2"); expect(container.textContent).not.toContain("/3");
  });

  it("opens an empty-cut live session without a signal history request", async () => {
    api.getSession.mockResolvedValue(session()); await render();
    expect(api.getSessionSignalPage).not.toHaveBeenCalled();
    expect(messages).toHaveLength(1); expect(signals[0]!.options?.initialAfterSeq).toBe(0);
  });

  it("keeps older-server compatibility after detail resolves without a live snapshot", async () => {
    const detail = deferred<SessionDetail>(); api.getSession.mockReturnValueOnce(detail.promise);
    await render(); expect(messages).toHaveLength(0);
    await settle(() => detail.resolve(session("s1", null)));
    expect(messages[0]!.options).toBeUndefined(); expect(signals[0]!.options).toBeUndefined();
    expect(api.getSessionSignalPage).not.toHaveBeenCalled();
  });

  it("rejects a malformed explicit snapshot visibly and never opens legacy replay", async () => {
    api.getSession.mockResolvedValue({ ...session(), stream_snapshot: { version: 2 } });
    await render(); expect(container.textContent).toMatch(/snapshot/i);
    expect(messages).toHaveLength(0); expect(signals).toHaveLength(0);
  });

  it("coalesces both channels into one paired resnapshot and ignores old callbacks", async () => {
    const fresh = deferred<SessionDetail>();
    api.getSession.mockResolvedValueOnce(session()).mockReturnValueOnce(fresh.promise);
    await render(); const oldMessages = messages[0]!; const oldSignals = signals[0]!;
    await settle(() => {
      overflow(oldMessages);
      oldSignals.options!.onResyncRequired!({ type: "resync_required", stream: "signals", reason: "overflow", resume_after_seq: 99 }, { afterSeq: 0 });
      oldMessages.event({ ...message("stale-old-frame"), branch_id: "b1" });
    });
    expect(api.getSession).toHaveBeenCalledTimes(2);
    expect(oldMessages.stop).toHaveBeenCalled(); expect(oldSignals.stop).toHaveBeenCalled();
    expect(messages).toHaveLength(1); expect(container.textContent).not.toContain("stale-old-frame");
    api.getSessionSignalPage.mockResolvedValueOnce(page(1));
    await settle(() => fresh.resolve(session("s1", "replacement-ticket", 1)));
    expect(messages).toHaveLength(2); expect(signals).toHaveLength(2);
    expect(messages[1]!.options?.initialCursor).toBe("replacement-ticket");
    expect(signals[1]!.options).toMatchObject({ initialSnapshot: "replacement-ticket", initialAfterSeq: 1 });
    expect(api.getSession).toHaveBeenLastCalledWith("s1", { liveSnapshot: true });
  });

  it("drops a first-visit A bootstrap after navigating A to B to A", async () => {
    const oldA = deferred<SessionDetail>();
    api.getSession.mockReturnValueOnce(oldA.promise).mockResolvedValueOnce(session("B", "B-ticket")).mockResolvedValueOnce(session("A", "new-A-ticket"));
    await render("A"); await render("B"); await render("A");
    await settle(() => oldA.resolve({ ...session("A", "stale-A-ticket"), name: "stale-first-A" }));
    expect(messages.map((subscription) => subscription.options?.initialCursor)).toEqual(["B-ticket", "new-A-ticket"]);
    expect(container.textContent).toContain("name-A"); expect(container.textContent).not.toContain("stale-first-A");
  });

  it("discards an old prefix that finishes after switching sessions", async () => {
    const oldPrefix = deferred<SessionSignalPage>();
    api.getSession.mockResolvedValueOnce(session("A", "A-ticket", 1)).mockResolvedValueOnce(session("B", "B-ticket"));
    api.getSessionSignalPage.mockReturnValueOnce(oldPrefix.promise);
    await render("A"); await render("B");
    await settle(() => oldPrefix.resolve(page(1, "A")));
    expect(messages.map((subscription) => subscription.id)).toEqual(["B"]);
    expect(signals.map((subscription) => subscription.id)).toEqual(["B"]);
  });

  it("rejects an older-page response from before resnapshot without changing its new anchor", async () => {
    const oldPage = deferred<SessionDetail>();
    const initial = { ...session(), message_next_cursor: "old-anchor", branches: [
      { ...session().branches[0]!, message_total: 2 },
    ] };
    const fresh = { ...session("s1", "new-ticket"), message_next_cursor: "new-anchor", branches: [
      { ...session().branches[0]!, message_total: 2 },
    ] };
    api.getSession.mockResolvedValueOnce(initial).mockReturnValueOnce(oldPage.promise).mockResolvedValueOnce(fresh);
    await render(); expect(olderButton()).toBeDefined();
    await settle(() => olderButton()!.click());
    await settle(() => overflow());
    const stale = { ...session(), message_next_cursor: "stale-anchor", branches: [{ ...initial.branches[0]!, messages: [message("old-page-only")] }] };
    await settle(() => oldPage.resolve(stale));
    expect(container.textContent).not.toContain("old-page-only");
    api.getSession.mockResolvedValueOnce({ ...fresh, message_next_cursor: null });
    await settle(() => olderButton()!.click());
    expect(api.getSession).toHaveBeenLastCalledWith("s1", { messageCursor: "new-anchor" });
  });

  it("replaces unproven old-only rows and reaches them through the fresh backward cursor", async () => {
    const initial = { ...session(), message_next_cursor: "older", branches: [
      { ...session().branches[0]!, message_total: 2 },
    ] };
    const older = { ...session(), message_next_cursor: null, branches: [{ ...initial.branches[0]!, message_total: 2, messages: [message("retained-old-message")] }] };
    const fresh = { ...session("s1", "next-ticket"), message_next_cursor: "fresh-older", branches: [{ ...initial.branches[0]!, message_total: 3, messages: [message("initial-message"), message("fresh-tail-message")] }] };
    const authoritativeOldPage = { ...fresh, message_next_cursor: null, branches: [{ ...fresh.branches[0]!, messages: [message("retained-old-message")] }] };
    api.getSession.mockResolvedValueOnce(initial).mockResolvedValueOnce(older).mockResolvedValueOnce(fresh)
      .mockResolvedValueOnce(authoritativeOldPage);
    await render(); await settle(() => olderButton()!.click()); await settle(() => overflow());
    expect(container.textContent).not.toContain("retained-old-message");
    expect(container.textContent).toContain("fresh-tail-message");
    await settle(() => olderButton()!.click());
    expect(api.getSession).toHaveBeenLastCalledWith("s1", { messageCursor: "fresh-older" });
    expect(container.textContent).toContain("3/3");
    await settle(() => container.querySelector<HTMLButtonElement>('button[aria-label="Previous message"]')!.click());
    await settle(() => container.querySelector<HTMLButtonElement>('button[aria-label="Previous message"]')!.click());
    expect(container.textContent).toContain("retained-old-message");
    expect(container.textContent).toContain("/3");
  });

  it("does not silently reopen replay when resnapshot fails", async () => {
    api.getSession.mockResolvedValueOnce(session()).mockRejectedValueOnce(new Error("snapshot unavailable"));
    await render(); await settle(() => overflow());
    expect(container.textContent).toContain("snapshot unavailable");
    expect(messages).toHaveLength(1); expect(signals).toHaveLength(1);
    expect(api.getSession).toHaveBeenCalledTimes(2);
  });

  it("uses an ordinary terminal refresh once without rebasing either live cursor", async () => {
    api.getSession.mockResolvedValueOnce(session()).mockResolvedValueOnce({ ...session("s1", null), status: "completed" });
    await render();
    await settle(() => { messages[0]!.event({ type: "done" }); messages[0]!.event({ type: "done" }); });
    expect(api.getSession).toHaveBeenCalledTimes(2);
    expect(api.getSession).toHaveBeenLastCalledWith("s1");
    expect(messages).toHaveLength(1); expect(signals).toHaveLength(1);
    expect(signals[0]!.options?.initialSnapshot).toBe("ticket-one");
  });
});


function resyncReason(reason: string, subscription = messages.at(-1)!) {
  subscription.options!.onResyncRequired!(
    { type: "resync_required", stream: "messages", reason, resume_cursor: "advisory" },
    { cursor: subscription.options!.initialCursor },
  );
}
function authoredGraph(prefix: string) {
  const node = (suffix: string) => ({ id: `${prefix}-${suffix}`, label: `${prefix}-${suffix}`,
    role: "", assignment: "", prompt: "", capacity: 1, timeout: null, inputs: [], outputs: [] });
  return { nodes: [node("A"), node("B")],
    edges: [{ id: "edge", source: `${prefix}-A`, target: `${prefix}-B`, mode: "simple" }] };
}

describe("paired bootstrap recovery safety", () => {
  it.each(["resource_limit", "expired"])("bounds repeated %s resync without accepted data", async (reason) => {
    api.getSession.mockResolvedValueOnce(session()).mockResolvedValueOnce(session("s1", "replacement"));
    await render();
    await settle(() => resyncReason(reason));
    expect(messages).toHaveLength(2); expect(signals).toHaveLength(2);
    const latestMessages = messages[1]!; const latestSignals = signals[1]!;
    await settle(() => resyncReason(reason));
    expect(api.getSession).toHaveBeenCalledTimes(2);
    expect(latestMessages.stop).toHaveBeenCalled(); expect(latestSignals.stop).toHaveBeenCalled();
    expect(container.textContent).toContain("Live updates could not recover");
  });

  it.each(["messages", "signals"] as const)("accepted %s data permits another single recovery", async (channel) => {
    api.getSession.mockResolvedValueOnce(session()).mockResolvedValueOnce(session("s1", "second"))
      .mockResolvedValueOnce(session("s1", "third"));
    await render(); await settle(() => overflow());
    await settle(() => {
      if (channel === "messages") messages[1]!.event({ ...message("accepted"), branch_id: "b1" });
      else signals[1]!.event({ id: "accepted-signal", session_id: "s1", seq: 1, kind: "NodeStarted",
        op_id: "worker", ts: 1_000, payload: { name: "worker" } });
    });
    await settle(() => overflow());
    expect(api.getSession).toHaveBeenCalledTimes(3);
    expect(messages.at(-1)!.options?.initialCursor).toBe("third");
  });

  it("heartbeats and stale-generation callbacks cannot reset the recovery budget", async () => {
    api.getSession.mockResolvedValueOnce(session()).mockResolvedValueOnce(session("s1", "replacement"));
    await render(); const oldMessages = messages[0]!; const oldSignals = signals[0]!;
    await settle(() => overflow());
    await settle(() => {
      messages[1]!.event({ type: "heartbeat" }); signals[1]!.event({ type: "heartbeat" });
      oldMessages.event({ ...message("stale"), branch_id: "b1" });
      oldSignals.event({ id: "stale", session_id: "s1", seq: 1, kind: "NodeStarted", op_id: "old", ts: 1_000, payload: {} });
    });
    await settle(() => overflow());
    expect(api.getSession).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain("Live updates could not recover");
  });

  it.each(["frontier_shrunk", "anchor_mismatch", "foreign_epoch"])("%s replaces incompatible loaded history", async (reason) => {
    const initial = { ...session(), branches: [
      { ...session().branches[0]!, message_total: 2, messages: [message("old-one"), message("old-two")] },
      { id: "retired", name: "retired", created_at: 1, message_total: 1, messages: [message("retired-message")] },
    ] };
    const fresh = { ...session("s1", "fresh"), branches: [
      { ...session().branches[0]!, message_total: 1, messages: [message("fresh-only")] },
    ] };
    api.getSession.mockResolvedValueOnce(initial).mockResolvedValueOnce(fresh);
    await render(); await settle(() => resyncReason(reason));
    expect(container.textContent).toContain("fresh-only");
    for (const old of ["old-one", "old-two", "retired-message"]) expect(container.textContent).not.toContain(old);
  });

  it("ordinary overflow drops a branch absent from fresh authoritative detail", async () => {
    const initial = { ...session(), branches: [...session().branches,
      { id: "retired", name: "retired", created_at: 1, message_total: 1, messages: [message("retired-message")] }] };
    api.getSession.mockResolvedValueOnce(initial).mockResolvedValueOnce(session("s1", "fresh"));
    await render(); await settle(() => overflow());
    expect(container.textContent).not.toContain("retired-message");
  });

  it("resnapshot installs the new authored graph", async () => {
    window.history.replaceState(null, "", "/?view=graph");
    api.getSession.mockResolvedValueOnce({ ...session(), graph: authoredGraph("old") })
      .mockResolvedValueOnce({ ...session("s1", "fresh"), graph: authoredGraph("new") });
    await render(); await settle(() => overflow());
    expect(canvas.mock.calls.at(-1)![0].graph.nodes.map((node: { id: string }) => node.id)).toEqual(["new-A", "new-B"]);
  });

  it("fresh running metadata clears the previous terminal/live state before data arrives", async () => {
    window.history.replaceState(null, "", "/?view=graph");
    api.getSession.mockResolvedValueOnce({ ...session(), status: "completed", graph: authoredGraph("same") })
      .mockResolvedValueOnce({ ...session("s1", "fresh"), status: "running", graph: authoredGraph("same") });
    await render(); expect(canvas.mock.calls.at(-1)![0]).toMatchObject({ done: true, live: false });
    await settle(() => overflow());
    expect(canvas.mock.calls.at(-1)![0]).toMatchObject({ done: false, live: true });
  });

  it("ordinary reload cannot erase a live frame accepted while its GET was in flight", async () => {
    const { ApiError } = await import("@/lib/api");
    const reload = deferred<SessionDetail>();
    const initial = { ...session(), message_next_cursor: "stale-older", branches: [
      { ...session().branches[0]!, message_total: 2 },
    ] };
    api.getSession.mockResolvedValueOnce(initial).mockRejectedValueOnce(new ApiError(400, "stale anchor"))
      .mockReturnValueOnce(reload.promise);
    await render(); await settle(() => olderButton()!.click());
    const reloadButton = [...container.querySelectorAll("button")].find((button) => /reload conversation/i.test(button.textContent ?? ""));
    expect(reloadButton).toBeDefined();
    await settle(() => reloadButton!.click());
    await settle(() => messages[0]!.event({ ...message("accepted-during-reload"), branch_id: "b1" }));
    await settle(() => reload.resolve({ ...session("s1", null), message_next_cursor: "stale-result" }));
    expect(container.textContent).toContain("accepted-during-reload");
    expect([...container.querySelectorAll("button")].some((button) => /reload conversation/i.test(button.textContent ?? ""))).toBe(true);
    expect(messages).toHaveLength(1); expect(signals).toHaveLength(1);
  });
});


it("does not turn an unrecognized snapshot status into healthy live activity", async () => {
  window.history.replaceState(null, "", "/?view=graph");
  api.getSession.mockResolvedValueOnce({ ...session(), status: "future_status", graph: authoredGraph("unknown") });
  await render();
  expect(canvas.mock.calls.at(-1)![0]).toMatchObject({ live: false, done: false });
});
